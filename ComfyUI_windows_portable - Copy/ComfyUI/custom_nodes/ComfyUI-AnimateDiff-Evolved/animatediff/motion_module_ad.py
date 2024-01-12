import math
from typing import Iterable, Union

import torch
from einops import rearrange, repeat
from torch import Tensor, nn

from comfy.ldm.modules.attention import FeedForward
from .motion_lora import MotionLoRAInfo
from .motion_utils import GenericMotionWrapper, GroupNormAD, InjectorVersion, BlockType, CrossAttentionMM, MotionCompatibilityError, TemporalTransformerGeneric, prepare_mask_batch


def zero_module(module):
    # Zero out the parameters of a module and return it.
    for p in module.parameters():
        p.detach().zero_()
    return module


def get_ad_temporal_position_encoding_max_len(mm_state_dict: dict[str, Tensor], mm_type: str) -> int:
    # use pos_encoder.pe entries to determine max length - [1, {max_length}, {320|640|1280}]
    for key in mm_state_dict.keys():
        if key.endswith("pos_encoder.pe"):
            return mm_state_dict[key].size(1) # get middle dim
    raise MotionCompatibilityError(f"No pos_encoder.pe found in mm_state_dict - {mm_type} is not a valid AnimateDiff-SD1.5 motion module!")


def validate_ad_block_count(mm_state_dict: dict[str, Tensor], mm_type: str) -> None:
    # keep track of biggest down_block count in module
    biggest_block = 0
    for key in mm_state_dict.keys():
        if "down_blocks" in key:
            try:
                block_int = key.split(".")[1]
                block_num = int(block_int)
                if block_num > biggest_block:
                    biggest_block = block_num
            except ValueError:
                pass
    if biggest_block != 3:
        raise MotionCompatibilityError(f"Expected biggest down_block to be 3, but was {biggest_block} - {mm_type} is not a valid AnimateDiff-SD1.5 motion module!")


def has_mid_block(mm_state_dict: dict[str, Tensor]):
    # check if keys contain mid_block
    for key in mm_state_dict.keys():
        if key.startswith("mid_block."):
            return True
    return False


class AnimDiffMotionWrapper(GenericMotionWrapper):
    def __init__(self, mm_state_dict: dict[str, Tensor], mm_hash: str, mm_name: str="mm_sd_v15.ckpt" , loras: list[MotionLoRAInfo]=None):
        super().__init__(mm_hash, mm_name, loras)
        self.down_blocks: Iterable[MotionModule] = nn.ModuleList([])
        self.up_blocks: Iterable[MotionModule] = nn.ModuleList([])
        self.mid_block: Union[MotionModule, None] = None
        self.encoding_max_len = get_ad_temporal_position_encoding_max_len(mm_state_dict, mm_name)
        validate_ad_block_count(mm_state_dict, mm_name)
        for c in (320, 640, 1280, 1280):
            self.down_blocks.append(MotionModule(c, temporal_position_encoding_max_len=self.encoding_max_len, block_type=BlockType.DOWN))
        for c in (1280, 1280, 640, 320):
            self.up_blocks.append(MotionModule(c, temporal_position_encoding_max_len=self.encoding_max_len, block_type=BlockType.UP))
        if has_mid_block(mm_state_dict):
            self.mid_block = MotionModule(1280, temporal_position_encoding_max_len=self.encoding_max_len, block_type=BlockType.MID)
        self.mm_hash = mm_hash
        self.mm_name = mm_name
        self.version = "v1" if self.mid_block is None else "v2"
        self.injector_version = InjectorVersion.V1_V2
        self.AD_video_length: int = 24
        self.loras = loras
    
    def has_loras(self):
        # TODO: fix this to return False if has an empty list as well
        # but only after implementing a fix for lowvram loading
        return self.loras is not None
    
    def set_video_length(self, video_length: int, full_length: int):
        self.AD_video_length = video_length
        for block in self.down_blocks:
            block.set_video_length(video_length, full_length)
        for block in self.up_blocks:
            block.set_video_length(video_length, full_length)
        if self.mid_block is not None:
            self.mid_block.set_video_length(video_length, full_length)
    
    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for block in self.down_blocks:
            block.set_scale_multiplier(multiplier)
        for block in self.up_blocks:
            block.set_scale_multiplier(multiplier)
        if self.mid_block is not None:
            self.mid_block.set_scale_multiplier(multiplier)

    def set_masks(self, masks: Tensor, min_val: float, max_val: float):
        for block in self.down_blocks:
            block.set_masks(masks, min_val, max_val)
        for block in self.up_blocks:
            block.set_masks(masks, min_val, max_val)
        if self.mid_block is not None:
            self.mid_block.set_masks(masks, min_val, max_val)

    def set_sub_idxs(self, sub_idxs: list[int]):
        for block in self.down_blocks:
            block.set_sub_idxs(sub_idxs)
        for block in self.up_blocks:
            block.set_sub_idxs(sub_idxs)
        if self.mid_block is not None:
            self.mid_block.set_sub_idxs(sub_idxs)
    
    def reset_temp_vars(self):
        for block in self.down_blocks:
            block.reset_temp_vars()
        for block in self.up_blocks:
            block.reset_temp_vars()
        if self.mid_block is not None:
            self.mid_block.reset_temp_vars()


class MotionModule(nn.Module):
    def __init__(self, in_channels, temporal_position_encoding_max_len=24, block_type: str=BlockType.DOWN):
        super().__init__()
        if block_type == BlockType.MID:
            # mid blocks contain only a single VanillaTemporalModule
            self.motion_modules: Iterable[VanillaTemporalModule] = nn.ModuleList([get_motion_module(in_channels, temporal_position_encoding_max_len)])
        else:
            # down blocks contain two VanillaTemporalModules
            self.motion_modules = nn.ModuleList(
                [
                    get_motion_module(in_channels, temporal_position_encoding_max_len),
                    get_motion_module(in_channels, temporal_position_encoding_max_len)
                ]
            )
            # up blocks contain one additional VanillaTemporalModule
            if block_type == BlockType.UP: 
                self.motion_modules.append(get_motion_module(in_channels, temporal_position_encoding_max_len))
    
    def set_video_length(self, video_length: int, full_length: int):
        for motion_module in self.motion_modules:
            motion_module.set_video_length(video_length, full_length)
    
    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for motion_module in self.motion_modules:
            motion_module.set_scale_multiplier(multiplier)
    
    def set_masks(self, masks: Tensor, min_val: float, max_val: float):
        for motion_module in self.motion_modules:
            motion_module.set_masks(masks, min_val, max_val)
    
    def set_sub_idxs(self, sub_idxs: list[int]):
        for motion_module in self.motion_modules:
            motion_module.set_sub_idxs(sub_idxs)

    def reset_temp_vars(self):
        for motion_module in self.motion_modules:
            motion_module.reset_temp_vars()


def get_motion_module(in_channels, temporal_position_encoding_max_len):
    return VanillaTemporalModule(in_channels=in_channels, temporal_position_encoding_max_len=temporal_position_encoding_max_len)


class VanillaTemporalModule(nn.Module):
    def __init__(
        self,
        in_channels,
        num_attention_heads=8,
        num_transformer_block=1,
        attention_block_types=("Temporal_Self", "Temporal_Self"),
        cross_frame_attention_mode=None,
        temporal_position_encoding=True,
        temporal_position_encoding_max_len=24,
        temporal_attention_dim_div=1,
        zero_initialize=True,
    ):
        super().__init__()

        self.temporal_transformer = TemporalTransformer3DModel(
            in_channels=in_channels,
            num_attention_heads=num_attention_heads,
            attention_head_dim=in_channels
            // num_attention_heads
            // temporal_attention_dim_div,
            num_layers=num_transformer_block,
            attention_block_types=attention_block_types,
            cross_frame_attention_mode=cross_frame_attention_mode,
            temporal_position_encoding=temporal_position_encoding,
            temporal_position_encoding_max_len=temporal_position_encoding_max_len,
        )

        if zero_initialize:
            self.temporal_transformer.proj_out = zero_module(
                self.temporal_transformer.proj_out
            )

    def set_video_length(self, video_length: int, full_length: int):
        self.temporal_transformer.set_video_length(video_length, full_length)
    
    def set_scale_multiplier(self, multiplier: Union[float, None]):
        self.temporal_transformer.set_scale_multiplier(multiplier)

    def set_masks(self, masks: Tensor, min_val: float, max_val: float):
        self.temporal_transformer.set_masks(masks, min_val, max_val)
    
    def set_sub_idxs(self, sub_idxs: list[int]):
        self.temporal_transformer.set_sub_idxs(sub_idxs)

    def reset_temp_vars(self):
        self.temporal_transformer.reset_temp_vars()

    def forward(self, input_tensor, encoder_hidden_states=None, attention_mask=None):
        return self.temporal_transformer(input_tensor, encoder_hidden_states, attention_mask)
        #portion = output_tensor.shape[2] // 4 + output_tensor.shape[2] // 2
        portion = output_tensor.shape[2] // 2
        ad_effect = 0.7
        #output_tensor[:,:,portion:] = input_tensor[:,:,portion:] * (1-ad_effect) + output_tensor[:,:,portion:] * ad_effect
        #output_tensor[:,:,portion:] = input_tensor[:,:,portion:] #* 0.5
        return output_tensor


class TemporalTransformer3DModel(nn.Module, TemporalTransformerGeneric):
    def __init__(
        self,
        in_channels,
        num_attention_heads,
        attention_head_dim,
        num_layers,
        attention_block_types=(
            "Temporal_Self",
            "Temporal_Self",
        ),
        dropout=0.0,
        norm_num_groups=32,
        cross_attention_dim=768,
        activation_fn="geglu",
        attention_bias=False,
        upcast_attention=False,
        cross_frame_attention_mode=None,
        temporal_position_encoding=False,
        temporal_position_encoding_max_len=24,
    ):
        super().__init__()
        super().temporal_transformer_init(default_length=16)

        inner_dim = num_attention_heads * attention_head_dim

        self.norm = GroupNormAD(
            num_groups=norm_num_groups, num_channels=in_channels, eps=1e-6, affine=True
        )
        self.proj_in = nn.Linear(in_channels, inner_dim)

        self.transformer_blocks: Iterable[TemporalTransformerBlock] = nn.ModuleList(
            [
                TemporalTransformerBlock(
                    dim=inner_dim,
                    num_attention_heads=num_attention_heads,
                    attention_head_dim=attention_head_dim,
                    attention_block_types=attention_block_types,
                    dropout=dropout,
                    norm_num_groups=norm_num_groups,
                    cross_attention_dim=cross_attention_dim,
                    activation_fn=activation_fn,
                    attention_bias=attention_bias,
                    upcast_attention=upcast_attention,
                    cross_frame_attention_mode=cross_frame_attention_mode,
                    temporal_position_encoding=temporal_position_encoding,
                    temporal_position_encoding_max_len=temporal_position_encoding_max_len,
                )
                for d in range(num_layers)
            ]
        )
        self.proj_out = nn.Linear(inner_dim, in_channels)

    def set_video_length(self, video_length: int, full_length: int):
        self.video_length = video_length
        self.full_length = full_length
    
    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for block in self.transformer_blocks:
            block.set_scale_multiplier(multiplier)

    def set_masks(self, masks: Tensor, min_val: float, max_val: float):
        self.scale_min = min_val
        self.scale_max = max_val
        self.raw_scale_mask = masks

    def set_sub_idxs(self, sub_idxs: list[int]):
        self.sub_idxs = sub_idxs
        for block in self.transformer_blocks:
            block.set_sub_idxs(sub_idxs)

    def forward(self, hidden_states, encoder_hidden_states=None, attention_mask=None):
        batch, channel, height, width = hidden_states.shape
        residual = hidden_states

        scale_mask = self.get_scale_mask(hidden_states)

        hidden_states = self.norm(hidden_states)
        inner_dim = hidden_states.shape[1]
        hidden_states = hidden_states.permute(0, 2, 3, 1).reshape(
            batch, height * width, inner_dim
        )
        hidden_states = self.proj_in(hidden_states)

        # Transformer Blocks
        for block in self.transformer_blocks:
            hidden_states = block(
                hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                attention_mask=attention_mask,
                video_length=self.video_length,
                scale_mask=scale_mask
            )

        # output
        hidden_states = self.proj_out(hidden_states)
        hidden_states = (
            hidden_states.reshape(batch, height, width, inner_dim)
            .permute(0, 3, 1, 2)
            .contiguous()
        )

        output = hidden_states + residual

        return output


class TemporalTransformerBlock(nn.Module):
    def __init__(
        self,
        dim,
        num_attention_heads,
        attention_head_dim,
        attention_block_types=(
            "Temporal_Self",
            "Temporal_Self",
        ),
        dropout=0.0,
        norm_num_groups=32,
        cross_attention_dim=768,
        activation_fn="geglu",
        attention_bias=False,
        upcast_attention=False,
        cross_frame_attention_mode=None,
        temporal_position_encoding=False,
        temporal_position_encoding_max_len=24,
    ):
        super().__init__()

        attention_blocks = []
        norms = []

        for block_name in attention_block_types:
            attention_blocks.append(
                VersatileAttention(
                    attention_mode=block_name.split("_")[0],
                    context_dim=cross_attention_dim # called context_dim for ComfyUI impl
                    if block_name.endswith("_Cross")
                    else None,
                    query_dim=dim,
                    heads=num_attention_heads,
                    dim_head=attention_head_dim,
                    dropout=dropout,
                    #bias=attention_bias, # remove for Comfy CrossAttention
                    #upcast_attention=upcast_attention, # remove for Comfy CrossAttention
                    cross_frame_attention_mode=cross_frame_attention_mode,
                    temporal_position_encoding=temporal_position_encoding,
                    temporal_position_encoding_max_len=temporal_position_encoding_max_len,
                )
            )
            norms.append(nn.LayerNorm(dim))

        self.attention_blocks: Iterable[VersatileAttention] = nn.ModuleList(attention_blocks)
        self.norms = nn.ModuleList(norms)

        self.ff = FeedForward(dim, dropout=dropout, glu=(activation_fn == "geglu"))
        self.ff_norm = nn.LayerNorm(dim)

    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for block in self.attention_blocks:
            block.set_scale_multiplier(multiplier)

    def set_sub_idxs(self, sub_idxs: list[int]):
        for block in self.attention_blocks:
            block.set_sub_idxs(sub_idxs)

    def forward(
        self,
        hidden_states,
        encoder_hidden_states=None,
        attention_mask=None,
        video_length=None,
        scale_mask=None
    ):
        for attention_block, norm in zip(self.attention_blocks, self.norms):
            norm_hidden_states = norm(hidden_states)
            hidden_states = (
                attention_block(
                    norm_hidden_states,
                    encoder_hidden_states=encoder_hidden_states
                    if attention_block.is_cross_attention
                    else None,
                    attention_mask=attention_mask,
                    video_length=video_length,
                    scale_mask=scale_mask
                )
                + hidden_states
            )

        hidden_states = self.ff(self.ff_norm(hidden_states)) + hidden_states

        output = hidden_states
        return output


class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.0, max_len=24):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model)
        )
        pe = torch.zeros(1, max_len, d_model)
        pe[0, :, 0::2] = torch.sin(position * div_term)
        pe[0, :, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe)
        self.sub_idxs = None

    def set_sub_idxs(self, sub_idxs: list[int]):
        self.sub_idxs = sub_idxs

    def forward(self, x):
        #if self.sub_idxs is not None:
        #    x = x + self.pe[:, self.sub_idxs]
        #else:
        x = x + self.pe[:, : x.size(1)]
        return self.dropout(x)


class VersatileAttention(CrossAttentionMM):
    def __init__(
        self,
        attention_mode=None,
        cross_frame_attention_mode=None,
        temporal_position_encoding=False,
        temporal_position_encoding_max_len=24,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        assert attention_mode == "Temporal"

        self.attention_mode = attention_mode
        self.is_cross_attention = kwargs["context_dim"] is not None

        self.pos_encoder = (
            PositionalEncoding(
                kwargs["query_dim"],
                dropout=0.0,
                max_len=temporal_position_encoding_max_len,
            )
            if (temporal_position_encoding and attention_mode == "Temporal")
            else None
        )

    def extra_repr(self):
        return f"(Module Info) Attention_Mode: {self.attention_mode}, Is_Cross_Attention: {self.is_cross_attention}"

    def set_scale_multiplier(self, multiplier: Union[float, None]):
        if multiplier is None or math.isclose(multiplier, 1.0):
            self.scale = None
        else:
            self.scale = multiplier

    def set_sub_idxs(self, sub_idxs: list[int]):
        if self.pos_encoder != None:
            self.pos_encoder.set_sub_idxs(sub_idxs)

    def forward(
        self,
        hidden_states: Tensor,
        encoder_hidden_states=None,
        attention_mask=None,
        video_length=None,
        scale_mask=None,
    ):
        if self.attention_mode != "Temporal":
            raise NotImplementedError

        d = hidden_states.shape[1]
        hidden_states = rearrange(
            hidden_states, "(b f) d c -> (b d) f c", f=video_length
        )

        if self.pos_encoder is not None:
           hidden_states = self.pos_encoder(hidden_states)

        encoder_hidden_states = (
            repeat(encoder_hidden_states, "b n c -> (b d) n c", d=d)
            if encoder_hidden_states is not None
            else encoder_hidden_states
        )

        hidden_states = super().forward(
            hidden_states,
            encoder_hidden_states,
            value=None,
            mask=attention_mask,
            scale_mask=scale_mask,
        )

        hidden_states = rearrange(hidden_states, "(b d) f c -> (b f) d c", d=d)

        return hidden_states
