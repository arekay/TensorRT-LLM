from __future__ import annotations

import enum
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import torch
import torch.nn.functional as F
from torch import nn
from torch.nn.parameter import Parameter

import tensorrt_llm.quantization.utils.fp4_utils as fp4_utils
from tensorrt_llm._torch.peft.lora.layer import LoraLayer
from tensorrt_llm.functional import (AllReduceFusionOp, AllReduceParams,
                                     AllReduceStrategy)
from tensorrt_llm.mapping import Mapping
from tensorrt_llm.quantization.functional import \
    preprocess_weights_for_mixed_gemm
from tensorrt_llm.quantization.mode import QuantAlgo

from ...models.modeling_utils import QuantConfig
from ..utils import Fp4QuantizedTensor


class WeightMode(str, enum.Enum):
    # weight of a vanilla layer
    VANILLA = 'vanilla'
    # weight of a fused QKV linear layer
    FUSED_QKV_LINEAR = 'fused_qkv_linear'
    # weight of a fused gate and up linear layer
    FUSED_GATE_UP_LINEAR = 'fused_gate_up_linear'


@dataclass(kw_only=True)
class WeightsLoadingConfig:
    weight_mode: WeightMode = WeightMode.VANILLA
    ignore_tensor_parallel: bool = False


class TensorParallelMode(str, enum.Enum):
    COLUMN = 'column'
    ROW = 'row'

    @classmethod
    def split_dim(cls, mode):
        return 1 if mode == cls.ROW else 0

    # Helper to shard the corresponding per-channel activation scales
    # Which shard along the dimension orthogonal to the weights
    @classmethod
    def flip(cls, mode):
        return cls.ROW if mode == cls.COLUMN else cls.COLUMN


def load_weight_shard(
        weight,
        tensor_parallel_size: int = 1,
        tensor_parallel_rank: int = 0,
        tensor_parallel_mode: Optional[TensorParallelMode] = None,
        device: torch.device = torch.device('cpu'),
) -> torch.Tensor:
    if isinstance(weight, torch.Tensor):
        tensor_shape = weight.shape

        def maybe_convert_to_torch_tensor(tensor: torch.Tensor,
                                          indices: slice = None):
            if indices is None:
                # Avoid unnecessary copy
                return tensor.to(device)
            else:
                return tensor[indices].to(device)
    # WAR to check whether it is a safetensor slice since safetensor didn't register the type to the module
    # safetensors slice, supports lazy loading, type(weight) is `builtin.PySafeSlice`
    elif hasattr(weight, "get_shape"):
        tensor_shape = weight.get_shape()

        def maybe_convert_to_torch_tensor(
            tensor, indices: Union[slice, tuple[slice]] = slice(None)):
            return tensor[indices].to(device)
    else:
        raise ValueError(f'unsupported weight type: {type(weight)}')
    if tensor_parallel_mode is None or tensor_parallel_size <= 1:
        return maybe_convert_to_torch_tensor(weight)

    split_dim = TensorParallelMode.split_dim(tensor_parallel_mode)

    if len(tensor_shape) == 1 and split_dim == 1:
        return maybe_convert_to_torch_tensor(weight)

    width = tensor_shape[split_dim]
    if width == 1:
        return maybe_convert_to_torch_tensor(weight)

    slice_width = math.ceil(width / tensor_parallel_size)
    slice_start = tensor_parallel_rank * slice_width
    slice_end = min((tensor_parallel_rank + 1) * slice_width, width)
    slice_obj = [slice(None)] * len(tensor_shape)
    slice_obj[split_dim] = slice(slice_start, slice_end)
    return maybe_convert_to_torch_tensor(weight, tuple(slice_obj))


def copy_weight(dst: Parameter, src: torch.Tensor):
    # TODO check that is it a reasonable change or not
    if dst.dtype != src.dtype:
        src = src.to(dst.dtype)
    assert dst.dtype == src.dtype, f"Incompatible dtype. dst: {dst.dtype}, src: {src.dtype}"
    dst.data.copy_(src)


def load_weights_vanilla_helper(module: Linear, weights: List[Dict]):
    assert len(weights) == 1
    device = torch.device('cuda')

    weight = load_weight_shard(weights[0]['weight'], module.tp_size,
                               module.tp_rank, module.tp_mode, device)

    if module.has_w4a16_awq or module.has_w4a8_awq:
        # NOTE: without the preprocess during the runtime, the gemm output nan's. in order to use the preprocess_weights_for_mixed_gemm
        # we need to cast the weight to int8 first.
        activation_dtype = torch.float16 if module.has_w4a16_awq else torch.float8_e4m3fn
        weight = preprocess_weights_for_mixed_gemm(
            weight.T.to(torch.int8).contiguous().cpu(), torch.quint4x2,
            activation_dtype).cuda().contiguous()

    copy_weight(module.weight, weight)

    if module.bias is not None:
        bias = load_weight_shard(weights[0]['bias'], module.tp_size,
                                 module.tp_rank, module.tp_mode, device)
        copy_weight(module.bias, bias)


def load_weights_fused_qkv_helper(
        module: Linear,
        weights: List[Dict]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    assert len(weights) == 3
    device = torch.device('cuda')

    q_weight = load_weight_shard(weights[0]['weight'], module.tp_size,
                                 module.tp_rank, module.tp_mode, device)
    k_weight = load_weight_shard(weights[1]['weight'], module.tp_size,
                                 module.tp_rank, module.tp_mode, device)
    v_weight = load_weight_shard(weights[2]['weight'], module.tp_size,
                                 module.tp_rank, module.tp_mode, device)

    if module.bias is not None:
        q_bias = load_weight_shard(weights[0]['bias'], module.tp_size,
                                   module.tp_rank, module.tp_mode, device)
        k_bias = load_weight_shard(weights[1]['bias'], module.tp_size,
                                   module.tp_rank, module.tp_mode, device)
        v_bias = load_weight_shard(weights[2]['bias'], module.tp_size,
                                   module.tp_rank, module.tp_mode, device)
        copy_weight(module.bias, torch.cat((q_bias, k_bias, v_bias)))

    return (q_weight, k_weight, v_weight)


def load_weights_fused_gate_up_helper(
        module: Linear,
        weights: List[Dict]) -> tuple[torch.Tensor, torch.Tensor]:
    assert len(weights) == 2
    device = torch.device('cuda')

    gate_weight = load_weight_shard(weights[0]['weight'], module.tp_size,
                                    module.tp_rank, module.tp_mode, device)
    up_weight = load_weight_shard(weights[1]['weight'], module.tp_size,
                                  module.tp_rank, module.tp_mode, device)
    if module.bias is not None:
        gate_bias = load_weight_shard(weights[0]['bias'], module.tp_size,
                                      module.tp_rank, module.tp_mode, device)
        up_bias = load_weight_shard(weights[1]['bias'], module.tp_size,
                                    module.tp_rank, module.tp_mode, device)
        copy_weight(module.bias, torch.cat((up_bias, gate_bias)))
    return (gate_weight, up_weight)


class LinearMethodBase(ABC):
    """
    Base class for all linear methods.
    """

    @abstractmethod
    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype, *args,
                       **kwargs):
        raise NotImplementedError

    @abstractmethod
    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor], *args, **kwargs):
        raise NotImplementedError

    def load_weights(self, module: Linear, weights: List[Dict],
                     weight_mode: WeightMode):
        """
        Load weights from the checkpoint.
        """
        if weight_mode == WeightMode.VANILLA:
            self.load_weights_vanilla(module, weights)
        elif weight_mode == WeightMode.FUSED_QKV_LINEAR:
            self.load_weights_fused_qkv_linear(module, weights)
        elif weight_mode == WeightMode.FUSED_GATE_UP_LINEAR:
            self.load_weights_fused_gate_up_linear(module, weights)
        else:
            raise ValueError(f'unsupported weight mode: {weight_mode}')

    def load_weight_scales(self, weights: List[Dict], *args, **kwargs):
        """
        Load quantized weight scales from the checkpoint.
        """

    @abstractmethod
    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        """
        Load weights for the VANILLA weight mode.
        """
        raise NotImplementedError

    @abstractmethod
    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        """
        Load weights for the FUSED_QKV_LINEAR weight mode.
        """
        raise NotImplementedError

    @abstractmethod
    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        """
        Load weights for the FUSED_GATE_UP_LINEAR weight mode.
        """
        raise NotImplementedError


class UnquantizedLinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        weight_shape = (out_features, in_features)
        module.weight = Parameter(torch.empty(weight_shape, dtype=dtype),
                                  requires_grad=False)

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        if module.use_custom_cublas_mm:
            output = torch.ops.trtllm.cublas_mm(input,
                                                module.weight.t(),
                                                bias,
                                                out_dtype=None)
        else:
            output = F.linear(input, module.weight, bias)
        return output

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)
        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        copy_weight(module.weight, fused_weight)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)
        fused_weight = torch.cat((gate_weight, up_weight))
        copy_weight(module.weight, fused_weight)


class FP8QDQLinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        weight_shape = (out_features, in_features)
        module.weight = Parameter(torch.empty(weight_shape,
                                              dtype=torch.float8_e4m3fn),
                                  requires_grad=False)
        module.weight_scale = Parameter(torch.tensor(1., dtype=torch.float32),
                                        requires_grad=False)
        module.input_scale = Parameter(torch.tensor(1., dtype=torch.float32),
                                       requires_grad=False)
        module.inv_input_scale = Parameter(torch.tensor(1.,
                                                        dtype=torch.float32),
                                           requires_grad=False)

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        cur_input_scale = module.input_scale
        if input.dtype != torch.float8_e4m3fn:
            if module.input_scale is not None and not module.force_dynamic_quantization:
                # Static quantization
                qinput, _ = torch.ops.tensorrt_llm.static_quantize_e4m3_per_tensor(
                    input, module.input_scale)
            else:
                # Dynamic quantization
                qinput, cur_input_scale = torch.ops.tensorrt_llm.quantize_e4m3_per_tensor(
                    input)
                cur_input_scale = cur_input_scale.to(torch.float32)

        else:
            qinput = input

        # This op does not support bias now.
        if qinput.shape[0] <= 8 and module.enable_cuda_core:
            # use cuda core for small m dimension
            output = torch.ops.trtllm.cuda_scaled_mm(
                qinput,
                module.weight.t(),
                scale_a=cur_input_scale,
                scale_b=module.weight_scale,
                bias=None,
                out_dtype=module.dtype or input.dtype,
            )
        else:
            output = torch.ops.trtllm.cublas_scaled_mm(
                qinput,
                module.weight.t(),
                scale_a=cur_input_scale,
                scale_b=module.weight_scale,
                bias=None,
                out_dtype=module.dtype or input.dtype,
            )
        if bias is not None:
            output = output + bias
        return output

    def load_weight_scales(self, weights: List[Dict]):
        input_scale, weight_scale = [], []
        for w in weights:
            if "input_scale" in w:
                input_scale.append(w["input_scale"][...].reshape([]))
            if "weight_scale" in w:
                weight_scale.append(w["weight_scale"][...].reshape([]))
        return input_scale, weight_scale

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)
        input_scale, weight_scale = self.load_weight_scales(weights)
        if len(input_scale) != 0:
            # Static quantization
            copy_weight(module.input_scale, input_scale[0])
            module.inv_input_scale.data = 1.0 / module.input_scale
        else:
            # Dynamic quantization
            module.input_scale = None
            module.inv_input_scale = None
        copy_weight(module.weight_scale, weight_scale[0])

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)

        input_scale, weight_scale = self.load_weight_scales(weights)
        if len(input_scale) != 0:
            # Static quantization
            copy_weight(module.input_scale, max(input_scale))
        else:
            # Dynamic quantization
            module.input_scale = None
        copy_weight(module.weight_scale, max(weight_scale))

        q_weight = q_weight.to(module.dtype) * weight_scale[0]
        k_weight = k_weight.to(module.dtype) * weight_scale[1]
        v_weight = v_weight.to(module.dtype) * weight_scale[2]

        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        if module.weight_scale.device != fused_weight.device:
            module.weight_scale = Parameter(
                module.weight_scale.data.to(fused_weight.device))
        fused_weight = (fused_weight / module.weight_scale).to(
            torch.float8_e4m3fn)
        copy_weight(module.weight, fused_weight)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        input_scale, weight_scale = self.load_weight_scales(weights)
        if len(input_scale) != 0:
            # Static quantization
            copy_weight(module.input_scale, max(input_scale))
        else:
            # Dynamic quantization
            module.input_scale = None
        copy_weight(module.weight_scale, max(weight_scale))

        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)

        gate_weight = gate_weight.to(module.dtype) * weight_scale[0]
        up_weight = up_weight.to(module.dtype) * weight_scale[1]
        fused_weight = torch.cat((gate_weight, up_weight))
        if module.weight_scale.device != fused_weight.device:
            module.weight_scale = Parameter(
                module.weight_scale.data.to(fused_weight.device))
        fused_weight = (fused_weight / module.weight_scale).to(
            torch.float8_e4m3fn)
        copy_weight(module.weight, fused_weight)


class FP8RowwiseLinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        weight_shape = (out_features, in_features)

        module.weight = Parameter(torch.empty(weight_shape,
                                              dtype=torch.float8_e4m3fn),
                                  requires_grad=False)
        module.weight_scale = Parameter(torch.empty(out_features),
                                        requires_grad=False)
        # Not really used for Gemm now.
        # Only used to quantize output of FP8 attention.
        module.input_scale = Parameter(torch.tensor(1., dtype=torch.float32),
                                       requires_grad=False)
        module.inv_input_scale = Parameter(torch.tensor(1.,
                                                        dtype=torch.float32),
                                           requires_grad=False)
        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        # FP8 tensor inputs are from attention. Directly use ones as scale.
        if input.dtype == torch.float8_e4m3fn:
            qinput = input
            cur_input_scale = torch.ones(input.shape[0],
                                         device=input.device,
                                         dtype=torch.float32)
        else:
            # Use dynamic per-token quantization for activation
            qinput, cur_input_scale = torch.ops.tensorrt_llm.quantize_e4m3_activation(
                input)

        # This op does not support bias now.
        output = torch.ops.trtllm.fp8_rowwise_gemm(
            qinput,
            module.weight,
            cur_input_scale.float(),
            module.weight_scale,
            module.dtype or input.dtype,
        )
        if bias is not None:
            output = output + bias
        return output

    def _get_scale_name(self, weights: List[Dict]):
        # `weight_scale_inv` for DS recipe and  `weight_scale` for ModelOpt recipe.
        # Actually they hold identical values of data_amax / 448.
        scale_name = "weight_scale_inv"
        if scale_name not in weights[0]:
            scale_name = "weight_scale"
        return scale_name

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]):
        load_weights_vanilla_helper(module, weights)

        scale_name = self._get_scale_name(weights)
        weight_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                         module.tp_rank, module.tp_mode)
        copy_weight(module.weight_scale, weight_scale)
        if "input_scale" in weights[0]:
            copy_weight(module.input_scale, weights[0]["input_scale"])
            module.inv_input_scale.data = 1.0 / module.input_scale

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]):
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)
        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        copy_weight(module.weight, fused_weight)

        scale_name = self._get_scale_name(weights)
        q_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        k_scale = load_weight_shard(weights[1][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        v_scale = load_weight_shard(weights[2][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        fused_fp8_block_scale = torch.cat((q_scale, k_scale, v_scale))
        copy_weight(module.weight_scale, fused_fp8_block_scale)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]):
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)
        fused_weight = torch.cat((gate_weight, up_weight))
        copy_weight(module.weight, fused_weight)

        scale_name = self._get_scale_name(weights)
        left_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                       module.tp_rank, module.tp_mode)
        right_scale = load_weight_shard(weights[1][scale_name], module.tp_size,
                                        module.tp_rank, module.tp_mode)
        fused_scale = torch.cat((left_scale, right_scale))
        copy_weight(module.weight_scale, fused_scale)


class FP8BlockScalesLinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        weight_shape = (out_features, in_features)

        module.weight = Parameter(torch.empty(weight_shape,
                                              dtype=torch.float8_e4m3fn),
                                  requires_grad=False)
        scale_shape = (math.ceil(out_features / 128),
                       math.ceil(in_features / 128))
        module.weight_scale = Parameter(torch.empty(scale_shape,
                                                    dtype=torch.float32),
                                        requires_grad=False)
        # Not really used for Gemm now.
        # Only used to quantize output of FP8 attention.
        module.input_scale = Parameter(torch.tensor(1., dtype=torch.float32),
                                       requires_grad=False)
        module.inv_input_scale = Parameter(torch.tensor(1.,
                                                        dtype=torch.float32),
                                           requires_grad=False)
        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        if input.dtype == torch.float8_e4m3fn:
            input = input.to(torch.bfloat16) * module.input_scale
        assert input.dtype == torch.bfloat16

        act_input_fp8, act_input_sf = torch.ops.trtllm.fp8_quantize_1x128(input)

        output = torch.ops.trtllm.fp8_block_scaling_gemm(
            act_input_fp8, module.weight, act_input_sf, module.weight_scale)
        if bias is not None:
            output = output + bias
        return output

    def _get_scale_name(self, weights: List[Dict]):
        # `weight_scale_inv` for DS recipe and  `weight_scale` for ModelOpt recipe.
        # Actually they hold identical values of data_amax / 448.
        scale_name = "weight_scale_inv"
        if scale_name not in weights[0]:
            scale_name = "weight_scale"
        return scale_name

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)

        scale_name = self._get_scale_name(weights)
        weight_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                         module.tp_rank,
                                         module.tp_mode).squeeze()
        copy_weight(module.weight_scale, weight_scale)
        if "input_scale" in weights[0]:
            copy_weight(module.input_scale, weights[0]["input_scale"])
            module.inv_input_scale.data = 1.0 / module.input_scale

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)
        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        copy_weight(module.weight, fused_weight)

        scale_name = self._get_scale_name(weights)
        q_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        k_scale = load_weight_shard(weights[1][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        v_scale = load_weight_shard(weights[2][scale_name], module.tp_size,
                                    module.tp_rank, module.tp_mode)
        fused_fp8_block_scale = torch.cat((q_scale, k_scale, v_scale)).squeeze()

        copy_weight(module.weight_scale, fused_fp8_block_scale)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)
        fused_weight = torch.cat((gate_weight, up_weight))
        copy_weight(module.weight, fused_weight)

        scale_name = self._get_scale_name(weights)
        left_scale = load_weight_shard(weights[0][scale_name], module.tp_size,
                                       module.tp_rank, module.tp_mode)
        right_scale = load_weight_shard(weights[1][scale_name], module.tp_size,
                                        module.tp_rank, module.tp_mode)
        fused_scale = torch.cat([left_scale, right_scale], dim=0).squeeze()
        copy_weight(module.weight_scale, fused_scale)


class NVFP4LinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        module.scaling_vector_size = 16
        assert in_features % module.scaling_vector_size == 0, f"in_features {in_features} must be divisible by scaling_vector_size {module.scaling_vector_size}"

        # Quantized weights
        module.weight = Parameter(torch.empty([out_features, in_features // 2],
                                              dtype=fp4_utils.float4_e2m1x2),
                                  requires_grad=False)

        # FP8 per-block scaling factors. dtype must be aligned with SF_DTYPE
        # Padding is required. See computeSFSize in quantization.h
        nrows = fp4_utils.pad_up(out_features, 128)
        ncols = fp4_utils.pad_up(in_features // module.scaling_vector_size, 4)
        module.weight_scale = Parameter(torch.empty(
            [nrows * ncols], dtype=fp4_utils.float4_sf_dtype),
                                        requires_grad=False)

        # FP32 per-tensor global scaling factor = 448*6/amax_input
        module.input_scale = Parameter(torch.empty([1], dtype=torch.float32),
                                       requires_grad=False)
        module.inv_input_scale = Parameter(torch.empty([1],
                                                       dtype=torch.float32),
                                           requires_grad=False)

        # (amax_input * amax_weight) / (448*6 * 448*6)
        module.alpha = Parameter(torch.empty([1], dtype=torch.float32),
                                 requires_grad=False)

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        if isinstance(input, Fp4QuantizedTensor):
            act_fp4, act_sf = input.fp4_tensor, input.scaling_factor
        else:
            act_fp4, act_sf = torch.ops.trtllm.fp4_quantize(
                input, module.input_scale, module.scaling_vector_size, False)

        output = torch.ops.trtllm.nvfp4_gemm(act_fp4, module.weight, act_sf,
                                             module.weight_scale, module.alpha,
                                             module.dtype)
        if bias is not None:
            output = output + bias
        return output

    def load_weight_scales(self,
                           weights: List[Dict],
                           tp_size: int = 1,
                           tp_rank: int = 0,
                           tp_mode: Optional[TensorParallelMode] = None):
        # For concatenated weights (qkv_proj / up_gate_proj), the global scaling factors and input scaling factors should be shared.
        input_scale = None
        weight_scale_2 = None
        weight_scale = []

        device = torch.device("cuda")

        for w in weights:
            if "input_scale" in w:
                if input_scale is None:
                    input_scale = w["input_scale"][...]
                else:
                    assert input_scale == w["input_scale"][
                        ...], "The input_scale should be same for all the weights"
            if "weight_scale" in w:
                ws = load_weight_shard(w["weight_scale"],
                                       tp_size,
                                       tp_rank,
                                       tp_mode,
                                       device=device).contiguous()
                assert ws.dtype == torch.float8_e4m3fn  # TODO: or e8m0 for mxfp4 recipe?
                weight_scale.append(ws.view(fp4_utils.float4_sf_dtype))
            if "weight_scale_2" in w:
                if weight_scale_2 is None:
                    weight_scale_2 = w["weight_scale_2"][...]
                else:
                    assert weight_scale_2 == w["weight_scale_2"][
                        ...], "The weight_scale_2 should be same for all the weights"

        # Compute scaling factor and alpha required by GEMM kernels
        # TODO: ModelOpt's o_proj.weight_scale_2 is bfloat16, which should be float32
        alpha = input_scale.float() * weight_scale_2.float()
        # modelopt ckpt stores amax/(448*6), convert to (448*6)/amax
        input_scale = 1.0 / input_scale

        return input_scale, weight_scale, alpha

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)

        input_scale, weight_scale, alpha = self.load_weight_scales(
            weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)

        assert len(weights) == 1
        weight_scale = weight_scale[0]
        # Swizzle weight scale
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)

        copy_weight(module.input_scale, input_scale)
        copy_weight(module.weight_scale, weight_scale)
        E2M1_MAX = 6.0
        module.inv_input_scale.data = module.input_scale / E2M1_MAX
        copy_weight(module.alpha, alpha)

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)

        input_scale, weight_scales, alpha = self.load_weight_scales(
            weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)
        # Swizzle weight scales after concatenation
        weight_scale = torch.cat(weight_scales, 0)
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)
        copy_weight(module.input_scale, input_scale)
        copy_weight(module.weight_scale, weight_scale)
        copy_weight(module.alpha, alpha)

        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        copy_weight(module.weight, fused_weight)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)
        fused_weight = torch.cat((gate_weight, up_weight))
        copy_weight(module.weight, fused_weight)

        input_scale, weight_scales, alpha = self.load_weight_scales(
            weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)
        # Swizzle weight scales after concatenation
        weight_scale = torch.cat(weight_scales, 0)
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)
        copy_weight(module.input_scale, input_scale)
        copy_weight(module.weight_scale, weight_scale)
        copy_weight(module.alpha, alpha)


class W4A8MXFP4FP8LinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        module.scaling_vector_size = 32
        assert module.in_features % module.scaling_vector_size == 0, f"in_features {module.in_features} must be divisible by scaling_vector_size {module.scaling_vector_size}"
        # Quantized weights
        module.weight = Parameter(torch.empty(
            [module.out_features, module.in_features // 2],
            dtype=fp4_utils.float4_e2m1x2),
                                  requires_grad=False)

        # FP8 per-block scaling factors. dtype must be aligned with SF_DTYPE
        # Padding is required. See computeSFSize in quantization.h
        nrows = fp4_utils.pad_up(module.out_features, 128)
        ncols = fp4_utils.pad_up(
            module.in_features // module.scaling_vector_size, 4)
        module.weight_scale = Parameter(torch.empty(
            [nrows * ncols], dtype=fp4_utils.float4_sf_dtype),
                                        requires_grad=False)

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        fp8_input, input_scale = torch.ops.tensorrt_llm.quantize_e4m3_per_tensor(
            input)
        input_scale = input_scale.to(torch.float32)
        nrows = fp4_utils.pad_up(input.shape[0], 128)
        ncols = fp4_utils.pad_up(input.shape[1] // module.scaling_vector_size,
                                 4)
        # 01111111 is 2^(127 - 127) = 1 in E8M0
        module.fake_act_scale = torch.empty(
            [nrows * ncols], dtype=torch.uint8,
            device=fp8_input.device).fill_(127).view(fp4_utils.float4_sf_dtype)
        output = torch.ops.trtllm.w4a8_mxfp4_fp8_gemm(fp8_input, module.weight,
                                                      module.fake_act_scale,
                                                      module.weight_scale,
                                                      input_scale, module.dtype)
        if bias is not None:
            output = output + bias
        return output

    def load_weight_scales(self,
                           weights: List[Dict],
                           tp_size: int = 1,
                           tp_rank: int = 0,
                           tp_mode: Optional[TensorParallelMode] = None):
        # For concatenated weights (qkv_proj / up_gate_proj), the global scaling factors and input scaling factors should be shared.
        weight_scale = []
        device = torch.device("cuda")
        for w in weights:
            if "weight_scale" in w:
                ws = load_weight_shard(w["weight_scale"],
                                       tp_size,
                                       tp_rank,
                                       tp_mode,
                                       device=device).contiguous()
                # Should be E8M0 for MXFP4
                assert ws.dtype == torch.uint8
                weight_scale.append(ws.view(fp4_utils.float4_sf_dtype))
        return weight_scale

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)

        weight_scale = self.load_weight_scales(weights,
                                               tp_size=module.tp_size,
                                               tp_rank=module.tp_rank,
                                               tp_mode=module.tp_mode)
        assert len(weights) == 1
        weight_scale = weight_scale[0]
        # Swizzle weight scale
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)
        copy_weight(module.weight_scale, weight_scale)

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)
        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        copy_weight(module.weight, fused_weight)

        weight_scale = self.load_weight_scales(weights,
                                               tp_size=module.tp_size,
                                               tp_rank=module.tp_rank,
                                               tp_mode=module.tp_mode)
        weight_scale = torch.cat(weight_scale, 0)
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)
        copy_weight(module.weight_scale, weight_scale)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)
        fused_weight = torch.cat((gate_weight, up_weight))
        copy_weight(module.weight, fused_weight)

        weight_scale = self.load_weight_scales(weights,
                                               tp_size=module.tp_size,
                                               tp_rank=module.tp_rank,
                                               tp_mode=module.tp_mode)
        # Swizzle weight scales after concatenation
        weight_scale = torch.cat(weight_scale, 0)
        weight_scale = torch.ops.trtllm.nvfp4_block_scale_interleave(
            weight_scale)
        copy_weight(module.weight_scale, weight_scale)


class W4A16_AWQ_LinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool,
                       dtype: torch.dtype) -> None:
        # Quantized weights
        module.weight = Parameter(torch.empty(
            (in_features, out_features // 2),
            dtype=torch.int8,
        ),
                                  requires_grad=False)

        group_size = module.quant_config.group_size
        if in_features % group_size != 0:
            raise ValueError(
                f"in_features ({self.in_features}) must be divisible by group_size ({group_size}) "
                f"for INT4 per-group quantization scale dimensions.")

        module.weight_scale = Parameter(torch.empty(
            (in_features // group_size, out_features), dtype=dtype),
                                        requires_grad=False)
        # NOTE: Not in all linear we have this tensor - pre_quant_scale is computed as an average and merged with the
        # LayerNorm for QKV and Gate/Up projection layers when possible. we can see the tensor only for o_proj and down_proj
        module.pre_quant_scale = None

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]) -> torch.Tensor:

        if module.pre_quant_scale is not None:
            input = input * module.pre_quant_scale

        bias = bias.contiguous() if bias is not None else None

        output = torch.ops.trtllm.finegrained_mixed_dtype_gemm(
            input=input.to(module.dtype).contiguous(),
            weight=module.weight,
            scales=module.weight_scale,
            group_size=module.quant_config.group_size,
            has_zero_point=module.quant_config.has_zero_point,
            output_dtype=module.dtype or input.dtype,
            bias=bias,
            zeros=None)
        return output

    def load_weight_scales(
            self,
            weights: List[Dict],
            tp_size: int = 1,
            tp_rank: int = 0,
            tp_mode: Optional[TensorParallelMode] = None) -> List[torch.Tensor]:
        device = torch.device("cuda")
        q_weight_scale = load_weight_shard(weights[0]['weight_scale'],
                                           tp_size,
                                           tp_rank,
                                           tp_mode,
                                           device=device)
        k_weight_scale = load_weight_shard(weights[1]['weight_scale'],
                                           tp_size,
                                           tp_rank,
                                           tp_mode,
                                           device=device)
        v_weight_scale = load_weight_shard(weights[2]['weight_scale'],
                                           tp_size,
                                           tp_rank,
                                           tp_mode,
                                           device=device)
        weight_scales = [q_weight_scale, k_weight_scale, v_weight_scale]

        return weight_scales

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]) -> None:
        load_weights_vanilla_helper(module, weights)

        device = torch.device('cuda')

        pre_quant_scale = load_weight_shard(
            weights[0]["pre_quant_scale"],
            module.tp_size,
            module.tp_rank,
            # pre_quant_scale applies to activation as opposed to weight, so flip tp_mode the other way around
            TensorParallelMode.flip(module.tp_mode),
            device,
        )

        module.pre_quant_scale = Parameter(
            torch.ones((module.in_features, ), dtype=pre_quant_scale.dtype),
            requires_grad=False).to(device=device)

        weight_scale = load_weight_shard(weights[0]['weight_scale'],
                                         module.tp_size, module.tp_rank,
                                         module.tp_mode, device)

        copy_weight(module.pre_quant_scale, pre_quant_scale)
        copy_weight(module.weight_scale, weight_scale.T.contiguous())

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]) -> None:
        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)

        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        fused_weight = preprocess_weights_for_mixed_gemm(
            fused_weight.to(torch.int8).T.contiguous().cpu(), torch.quint4x2,
            torch.float16).cuda().contiguous()

        copy_weight(module.weight, fused_weight)

        weight_scales = self.load_weight_scales(weights)

        # Create concatenated weight scale tensor
        cat_weight_scale = torch.cat(weight_scales, dim=0).T.contiguous()
        copy_weight(module.weight_scale, cat_weight_scale)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]) -> None:
        device = torch.device('cuda')
        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)

        fused_weight = torch.cat((gate_weight, up_weight))
        fused_weight = preprocess_weights_for_mixed_gemm(
            fused_weight.to(torch.int8).T.contiguous().cpu(), torch.quint4x2,
            torch.float16).cuda().contiguous()

        copy_weight(module.weight, fused_weight)

        left_scale = load_weight_shard(weights[0]['weight_scale'],
                                       module.tp_size, module.tp_rank,
                                       module.tp_mode, device).contiguous()
        right_scale = load_weight_shard(weights[1]['weight_scale'],
                                        module.tp_size, module.tp_rank,
                                        module.tp_mode, device).contiguous()
        fused_scale = torch.cat([left_scale, right_scale], dim=0).T.contiguous()
        copy_weight(module.weight_scale, fused_scale)


class W4A8_AWQ_LinearMethod(LinearMethodBase):

    def create_weights(self, module: Linear, in_features: int,
                       out_features: int, bias: bool, dtype: torch.dtype):
        # Quantized weights
        module.weight = Parameter(torch.empty(
            (in_features, out_features // 2),
            dtype=torch.int8,
        ),
                                  requires_grad=False)

        group_size = module.quant_config.group_size
        if in_features % group_size != 0:
            raise ValueError(
                f"in_features ({module.in_features}) must be divisible by group_size ({group_size}) "
                f"for INT4 per-group quantization scale dimensions.")

        # NOTE: for FP8 activation, scales needs to be float16
        module.weight_scale = Parameter(torch.empty(
            (in_features // group_size, out_features), dtype=torch.float16),
                                        requires_grad=False)

        # Similar to W4A16 AWQ, not all linears will have this tensor
        module.pre_quant_scale = None

        module.input_scale = Parameter(torch.tensor(1., dtype=torch.float32),
                                       requires_grad=False)
        module.inv_input_scale = Parameter(torch.tensor(1.,
                                                        dtype=torch.float32),
                                           requires_grad=False)

        module.alpha = Parameter(torch.empty([1], dtype=torch.float32),
                                 requires_grad=False)

        if bias:
            module.bias = Parameter(torch.empty((out_features), dtype=dtype),
                                    requires_grad=False)
        else:
            module.register_parameter("bias", None)

    def apply(self, module: Linear, input: torch.Tensor,
              bias: Optional[torch.Tensor]):
        """
        modelopt flow for w4a8_awq:
         1. multiply pre_quant_scale to input
         2. quantize input to fp8 using input_scale
         3. unpack_weights and multiply by weight_scales (int4 -> fp16)
         4. divied by weight_scale_2 (fp16 -> fp8 to allow gemm in fp8).
         5. apply gemm in fp8.
         6. rescale using alpha which is input_scale * weight_scale_2
        """
        if module.pre_quant_scale is not None:
            input = input * module.pre_quant_scale

        if input.dtype == torch.float8_e4m3fn:
            quantized_input = input
        else:
            quantized_input, _ = torch.ops.tensorrt_llm.static_quantize_e4m3_per_tensor(
                input, (module.input_scale))

        bias = bias.contiguous() if bias is not None else None

        output = torch.ops.trtllm.finegrained_mixed_dtype_gemm(
            input=quantized_input.contiguous(),
            weight=module.weight,
            scales=module.weight_scale,
            group_size=module.quant_config.group_size,
            has_zero_point=module.quant_config.has_zero_point,
            output_dtype=module.dtype
            or input.dtype,  # NOTE: output_dtype can only be bf16/fp16 for W4A8
            alpha=module.alpha.item(),
            bias=bias,
            zeros=None)

        return output

    def load_weight_scales_w4a8(self,
                                weights: List[Dict],
                                tp_size: int = 1,
                                tp_rank: int = 0,
                                tp_mode: Optional[TensorParallelMode] = None):
        # For concatenated weights (qkv_proj / up_gate_proj), the global scaling factors and input scaling factors should be shared.
        input_scale = None
        weight_scale_2 = None
        weight_scale = []

        device = torch.device("cuda")

        for w in weights:
            if "input_scale" in w:
                if input_scale is None:
                    input_scale = w["input_scale"][...]
                else:
                    assert input_scale == w["input_scale"][
                        ...], "The input_scale should be same for all the weights"
            if "weight_scale" in w:
                ws = load_weight_shard(w["weight_scale"],
                                       tp_size,
                                       tp_rank,
                                       tp_mode,
                                       device=device)

                weight_scale.append(ws.to(torch.float16))
            if "weight_scale_2" in w:
                if weight_scale_2 is None:
                    weight_scale_2 = w["weight_scale_2"][...]
                else:
                    assert weight_scale_2 == w["weight_scale_2"][
                        ...], "The weight_scale_2 should be same for all the weights"

        # Compute scaling factor and alpha required by GEMM kernels (rescale the gemm output in fp8)
        alpha = (input_scale.float() * weight_scale_2.float())

        return input_scale, weight_scale, alpha, weight_scale_2

    def load_weights_vanilla(self, module: Linear, weights: List[Dict]):
        load_weights_vanilla_helper(module, weights)

        device = torch.device('cuda')
        pre_quant_scale = load_weight_shard(
            weights[0]["pre_quant_scale"],
            module.tp_size,
            module.tp_rank,
            # pre_quant_scale applies to activation as opposed to weight, so flip tp_mode the other way around
            TensorParallelMode.flip(module.tp_mode),
            device,
        )

        assert pre_quant_scale.dtype == module.dtype

        module.pre_quant_scale = Parameter(
            torch.empty((module.in_features, ), dtype=pre_quant_scale.dtype),
            requires_grad=False).to(device=device)

        copy_weight(module.pre_quant_scale, pre_quant_scale)

        input_scale, weight_scale, alpha, weight_scale_2 = self.load_weight_scales_w4a8(
            weights=weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)

        assert len(weight_scale) == 1, "there should be only one weight scale"

        weight_scale = (weight_scale[0].T / weight_scale_2).contiguous()

        copy_weight(module.weight_scale, weight_scale)
        copy_weight(module.input_scale, input_scale)
        copy_weight(module.alpha, alpha)

        module.inv_input_scale.data = 1.0 / module.input_scale

    def load_weights_fused_qkv_linear(self, module: Linear,
                                      weights: List[Dict]):

        q_weight, k_weight, v_weight = load_weights_fused_qkv_helper(
            module, weights)

        fused_weight = torch.cat((q_weight, k_weight, v_weight))
        fused_weight = preprocess_weights_for_mixed_gemm(
            fused_weight.to(torch.int8).T.contiguous().cpu(), torch.quint4x2,
            torch.float8_e4m3fn).cuda().contiguous()

        copy_weight(module.weight, fused_weight)

        input_scale, weight_scales, alpha, weight_scale_2 = self.load_weight_scales_w4a8(
            weights=weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)

        # Create concatenated weight scale tensor
        cat_weight_scale = (torch.cat(weight_scales, dim=0).T /
                            weight_scale_2).contiguous()
        copy_weight(module.weight_scale, cat_weight_scale)
        copy_weight(module.input_scale, input_scale)
        copy_weight(module.alpha, alpha)

        # NOTE: pre_quant_scale is the same for q,k,v since modelopt checks which layer shared the same input and create an avg pre_quant_scale
        # Usually when modelopt exports the quantized model, pre_quant_Scale is fused in the layer norm (this case relevant if fused is disabled - modelopt internal)
        if "pre_quant_scale" in weights[0].keys():

            pre_quant_scale = load_weight_shard(
                weights[0]["pre_quant_scale"],
                module.tp_size,
                module.tp_rank,
                # pre_quant_scale applies to activation as opposed to weight, so flip tp_mode the other way around
                TensorParallelMode.flip(module.tp_mode),
                torch.device('cuda'),
            )

            module.pre_quant_scale = Parameter(
                torch.ones((module.in_features, ), dtype=pre_quant_scale.dtype),
                requires_grad=False).to(device=torch.device('cuda'))

            copy_weight(module.pre_quant_scale, pre_quant_scale)

    def load_weights_fused_gate_up_linear(self, module: Linear,
                                          weights: List[Dict]):

        gate_weight, up_weight = load_weights_fused_gate_up_helper(
            module, weights)

        fused_weight = torch.cat((gate_weight, up_weight))
        fused_weight = preprocess_weights_for_mixed_gemm(
            fused_weight.to(torch.int8).T.contiguous().cpu(), torch.quint4x2,
            torch.float8_e4m3fn).cuda().contiguous()

        copy_weight(module.weight, fused_weight)

        input_scale, weight_scale, alpha, weight_scale_2 = self.load_weight_scales_w4a8(
            weights=weights,
            tp_size=module.tp_size,
            tp_rank=module.tp_rank,
            tp_mode=module.tp_mode)

        fused_scale = (torch.cat(weight_scale, dim=0).T /
                       weight_scale_2).contiguous()
        copy_weight(module.weight_scale, fused_scale)
        copy_weight(module.input_scale, input_scale)
        copy_weight(module.alpha, alpha)

        if "pre_quant_scale" in weights[0].keys():
            pre_quant_scale = load_weight_shard(
                weights[0]["pre_quant_scale"],
                module.tp_size,
                module.tp_rank,
                # pre_quant_scale applies to activation as opposed to weight, so flip tp_mode the other way around
                TensorParallelMode.flip(module.tp_mode),
                torch.device('cuda'),
            )

            # NOTE:Create this tensor in load_weights, since not all layer have this tensor and memory is not allocated for it (same as W4A16)
            module.pre_quant_scale = Parameter(
                torch.ones((module.in_features, ), dtype=pre_quant_scale.dtype),
                requires_grad=False).to(device=torch.device('cuda'))

            copy_weight(module.pre_quant_scale, pre_quant_scale)


def get_quant_method(quant_config: Optional[QuantConfig] = None):
    if quant_config is None or not quant_config.layer_quant_mode.has_any_quant(
            exclude_kv_cache=True):
        return UnquantizedLinearMethod()
    if quant_config.layer_quant_mode.has_fp8_qdq():
        return FP8QDQLinearMethod()
    if quant_config.layer_quant_mode.has_fp8_rowwise():
        return FP8RowwiseLinearMethod()
    if quant_config.layer_quant_mode.has_fp8_block_scales():
        return FP8BlockScalesLinearMethod()
    if quant_config.layer_quant_mode.has_nvfp4():
        return NVFP4LinearMethod()
    if quant_config.layer_quant_mode.has_w4a8_mxfp4_fp8():
        return W4A8MXFP4FP8LinearMethod()
    if quant_config.layer_quant_mode.is_int4_weight_only_per_group(
    ) and quant_config.quant_algo == QuantAlgo.W4A16_AWQ:
        return W4A16_AWQ_LinearMethod()
    if quant_config.layer_quant_mode.is_int4_weight_only_per_group(
    ) and quant_config.quant_algo == QuantAlgo.W4A8_AWQ:
        return W4A8_AWQ_LinearMethod()
    raise ValueError(f'unsupported quant mode: {quant_config.quant_mode}')


class Linear(nn.Module):

    def __init__(
        self,
        in_features: int,
        out_features: int,
        bias: bool = True,
        dtype: torch.dtype = None,
        mapping: Optional[Mapping] = None,
        tensor_parallel_mode: Optional[TensorParallelMode] = None,
        gather_output: bool = False,  # COLUMN parallel only
        quant_config: Optional[QuantConfig] = None,
        weights_loading_config: Optional[WeightsLoadingConfig] = None,
        reduce_output: bool = True,  # ROW parallel only
        skip_create_weights_in_init: bool = False,
        use_custom_cublas_mm: bool = False,
        lora: Optional[LoraLayer] = None,
        allreduce_strategy: AllReduceStrategy = AllReduceStrategy.AUTO,
        force_dynamic_quantization: bool = False,
    ):
        from ..distributed import AllReduce

        super().__init__()
        self.has_bias = bias
        self.dtype = dtype
        self.mapping = mapping or Mapping()
        # could be modified later
        self.quant_config = quant_config
        self.weights_loading_config = weights_loading_config or WeightsLoadingConfig(
        )
        self.tp_size = self.mapping.tp_size
        self.tp_rank = self.mapping.tp_rank
        self.tp_mode = tensor_parallel_mode
        self.gather_output = gather_output
        self.force_dynamic_quantization = force_dynamic_quantization

        local_in_features = in_features
        local_out_features = out_features

        if self.tp_mode == TensorParallelMode.ROW:
            assert in_features % self.tp_size == 0, (
                f'in_features {in_features} must be divisible by tp_size {self.tp_size}'
            )
            local_in_features = in_features // self.tp_size
        elif self.tp_mode == TensorParallelMode.COLUMN:
            assert out_features % self.tp_size == 0, (
                f'out_features {out_features} must be divisible by tp_size {self.tp_size}'
            )
            local_out_features = out_features // self.tp_size
        else:
            assert self.tp_mode is None, (
                'unsupported tensor parallel mode: {self.tp_mode}')

        self.in_features = local_in_features
        self.out_features = local_out_features

        self.all_reduce = AllReduce(
            mapping=self.mapping,
            strategy=allreduce_strategy) if reduce_output else None
        self._weights_created = False
        self.reduce_output = reduce_output
        self.use_custom_cublas_mm = use_custom_cublas_mm
        self.lora = lora

        self.enable_cuda_core = False
        if torch.cuda.is_available():
            capability = torch.cuda.get_device_capability(
                torch.device('cuda:0'))
            # enable cuda core for sm89
            self.enable_cuda_core = capability[0] == 8 and capability[1] == 9

        if not skip_create_weights_in_init:
            self.create_weights()

    def create_weights(self):
        if self._weights_created:
            return

        self.quant_method = get_quant_method(self.quant_config)
        self.quant_method.create_weights(self, self.in_features,
                                         self.out_features, self.has_bias,
                                         self.dtype)

        self._weights_created = True

    @property
    def has_any_quant(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.has_any_quant(
            exclude_kv_cache=True)

    @property
    def has_fp8_qdq(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.has_fp8_qdq(
        )

    @property
    def has_fp8_rowwise(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.has_fp8_rowwise(
        )

    @property
    def has_fp8_block_scales(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.has_fp8_block_scales(
        )

    @property
    def has_nvfp4(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.has_nvfp4(
        )

    @property
    def has_w4a16_awq(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.is_int4_weight_only_per_group(
        ) and self.quant_config.quant_algo == QuantAlgo.W4A16_AWQ

    @property
    def has_w4a8_awq(self):
        assert self._weights_created
        return self.quant_config is not None and self.quant_config.layer_quant_mode.is_int4_weight_only_per_group(
        ) and self.quant_config.quant_algo == QuantAlgo.W4A8_AWQ

    def apply_linear(self,
                     input,
                     bias,
                     lora_params: Optional[dict] | None = None,
                     layer_idx: Optional[int] | None = None):
        output = self.quant_method.apply(self, input, bias)

        if self.lora is not None and bool(lora_params):
            lora_result = self.lora(input, lora_params, layer_idx)
            if lora_result is not None:
                output = output + lora_result
        return output

    def _maybe_fuse_bias_into_allreduce(
        self,
        bias: Optional[torch.Tensor],
        all_reduce_params: Optional[AllReduceParams] = None,
    ) -> bool:
        if self.tp_size > 1:
            fuse_bias_into_all_reduce = (
                bias is not None and all_reduce_params is not None
                and (all_reduce_params.fusion_op
                     == AllReduceFusionOp.RESIDUAL_RMS_NORM))
            if fuse_bias_into_all_reduce:
                all_reduce_params.bias = bias
                return True
        else:
            assert all_reduce_params is None or all_reduce_params.enable_allreduce is False, "Cannot fuse norm/residual/bias ops into allreduce op since we do not call allreduce op when tp_size is 1."
            return False

    def forward(
        self,
        input: Union[torch.Tensor, Fp4QuantizedTensor],
        *,
        all_reduce_params: Optional[AllReduceParams] = None,
        lora_params: Optional[dict] = None,
        layer_idx: Optional[int] = None,
    ) -> torch.Tensor:
        if self.tp_mode == TensorParallelMode.ROW:
            bias = None if (self.tp_rank > 0) else self.bias
            if self.reduce_output:
                fuse_bias = self._maybe_fuse_bias_into_allreduce(
                    bias, all_reduce_params)
                bias = None if fuse_bias else bias
                output = self.apply_linear(input, bias, lora_params, layer_idx)
                output = self.all_reduce(
                    output,
                    all_reduce_params=all_reduce_params,
                )
            else:
                output = self.apply_linear(input, bias, lora_params, layer_idx)
        elif self.tp_mode == TensorParallelMode.COLUMN:
            output = self.apply_linear(input, self.bias, lora_params, layer_idx)
            if self.gather_output:
                from ..distributed import allgather
                output = allgather(output, self.mapping)
        else:
            output = self.apply_linear(input, self.bias, lora_params, layer_idx)

        return output

    def load_weights(self, weights: List[Dict]):
        assert self._weights_created

        weight_mode = self.weights_loading_config.weight_mode
        self.quant_method.load_weights(self, weights, weight_mode)
