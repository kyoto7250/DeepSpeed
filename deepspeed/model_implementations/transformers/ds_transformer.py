'''
Copyright 2022 The Microsoft DeepSpeed Team
'''

import torch
from deepspeed.ops import op_builder
import torch.nn as nn
from deepspeed import comm as dist
from deepspeed.utils.logging import log_dist

from deepspeed.ops.transformer.inference.ds_mlp import DeepSpeedMLP
from deepspeed.ops.transformer.inference.ds_attention import DeepSpeedSelfAttention

inference_cuda_module = None


class DeepSpeedTransformerInference(nn.Module):
    """Initialize the DeepSpeed Transformer Layer.
        Arguments:
            layer_id: The layer index starting from 0, e.g. if model has 24 transformer layers,
                layer_id will be 0,1,2...23 when each layer object is instantiated
            config: An object of DeepSpeedInferenceConfig
            mp_group: Model parallelism group initialized on the modeling side.
            quantize_scales: This argument groups all the layers' scales used for quantization
            quantize_groups: Number of groups used for quantizing the model
            merge_count: Shows the number of model-parallel checkpoints merged before running inference.
                We use this argument to control the quantization scale for the model parameters if a bigger
                quantize-grouping than 1 is used.
            mlp_extra_grouping: This flag is used to show a 2x higher number of groups used for the MLP part
                of a Transformer layer. We use this feature for quantization to reduce the convergence impact
                for specific downstream tasks.
    """
    layer_id = 0

    def __init__(self,
                 config,
                 mp_group=None,
                 quantize_scales=None,
                 quantize_groups=1,
                 merge_count=1,
                 mlp_extra_grouping=False,
                 qkv_merging=False):
        super(DeepSpeedTransformerInference, self).__init__()

        self.config = config
        self.config.layer_id = DeepSpeedTransformerInference.layer_id
        DeepSpeedTransformerInference.layer_id += 1

        data_type = torch.half if config.fp16 else torch.float
        global inference_cuda_module
        if inference_cuda_module is None:
            builder = op_builder.InferenceBuilder()
            inference_cuda_module = builder.load()

        if DeepSpeedTransformerInference.layer_id == 1:
            log_dist(f"DeepSpeed-Inference config: {self.config.__dict__}", [0])

        self.attention = DeepSpeedSelfAttention(self.config,
                                                mp_group,
                                                quantize_scales,
                                                quantize_groups,
                                                merge_count,
                                                qkv_merging)
        self.mlp = DeepSpeedMLP(self.config,
                                mp_group,
                                quantize_scales,
                                quantize_groups,
                                merge_count,
                                mlp_extra_grouping)

        device = torch.cuda.current_device() if config.bigscience_bloom else 'cpu'
        self.norm_w = nn.Parameter(torch.empty(self.config.hidden_size,
                                               dtype=data_type,
                                               device=device),
                                   requires_grad=False)
        self.norm_b = nn.Parameter(torch.empty(self.config.hidden_size,
                                               dtype=data_type,
                                               device=device),
                                   requires_grad=False)
        self.layer_past = None
        self.allocate_workspace = inference_cuda_module.allocate_workspace_fp32 if (not config.fp16) else \
                                inference_cuda_module.allocate_workspace_fp16

    def forward(
            self,
            input,
            input_mask=None,
            attention_mask=None,
            head_mask=None,
            layer_past=None,
            get_key_value=False,
            get_present=False,
            encoder_output=None,
            enc_dec_attn_mask=None,
            encoder_hidden_states=None,
            encoder_attention_mask=None,
            use_cache=False,
            alibi=None,
            output_attentions=False,
            # TODO(arashb): 'layer_head_mask' and 'past_key_value' are only added to satisfy the OPT models API.
            # This needs to be redesigned later!
            layer_head_mask=None,
            past_key_value=None):
        # Allocate memory only on first layer forward
        if self.config.layer_id == 0:
            self.allocate_workspace(self.config.hidden_size,
                                    input.size()[0],
                                    input.size()[1],
                                    DeepSpeedTransformerInference.layer_id,
                                    self.config.heads,
                                    self.config.mp_size,
                                    self.config.bigscience_bloom,
                                    dist.get_rank() if dist.is_initialized() else 0,
                                    self.config.max_out_tokens)

        get_present = (get_present or get_key_value or use_cache)
        input_mask = input_mask if attention_mask is None else attention_mask

        # We set the prev key/value to None when there is a prompt
        if input.shape[1] > 1:
            self.layer_past = None
        layer_past = layer_past if layer_past is not None else self.layer_past
        head_mask = layer_head_mask if layer_head_mask is not None else head_mask

        attn_mask = None
        if isinstance(input, tuple):
            attn_mask = input[1]
            input = input[0]
        input_type = input.dtype

        if (self.config.fp16 or self.config.q_int8) \
            and input.dtype == torch.float:
            input = input.half()

        with torch.no_grad():
            attention_output, key, value, context_outputtn_ctx, inp_norm = \
                                     self.attention(input,
                                              input_mask,
                                              head_mask,
                                              layer_past,
                                              get_present,
                                              encoder_hidden_states,
                                              encoder_attention_mask,
                                              output_attentions,
                                              self.norm_w,
                                              self.norm_b,
                                              alibi)

            presents = (key, value)
            self.layer_past = presents if layer_past is None else None
            output = self.mlp(attention_output, input, inp_norm, self.attention.attn_ob)

            if not self.config.pre_layer_norm:
                ds_layernorm = inference_cuda_module.layer_norm_fp16 if self.config.fp16 or self.config.q_int8 else \
                                        inference_cuda_module.layer_norm_fp32
                output = ds_layernorm(output,
                                      self.norm_w,
                                      self.norm_b,
                                      self.config.epsilon)

            output = output.to(input_type)
        if get_present:
            output = (output, presents)

        if self.config.return_tuple:
            return output if type(output) is tuple else (output, attn_mask)
        else:
            return output
