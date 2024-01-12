# original HotShotXL components adapted from https://github.com/hotshotco/Hotshot-XL/blob/main/hotshot_xl/models/transformer_temporal.py
import math
from typing import Iterable, Optional, Union

import torch
from einops import rearrange, repeat
from torch import Tensor, nn

from comfy.ldm.modules.attention import FeedForward
from .motion_lora import MotionLoRAInfo
from .motion_utils import GenericMotionWrapper, GroupNormAD, InjectorVersion, BlockType, CrossAttentionMM, MotionCompatibilityError, TemporalTransformerGeneric


def zero_module(module):
    # Zero out the parameters of a module and return it.
    for p in module.parameters():
        p.detach().zero_()
    return module


def get_hsxl_temporal_position_encoding_max_len(mm_state_dict: dict[str, Tensor], mm_type: str) -> int:
    # use pos_encoder.positional_encoding entries to determine max length - [1, {max_length}, {320|640|1280}]
    for key in mm_state_dict.keys():
        if key.endswith("pos_encoder.positional_encoding"):
            return mm_state_dict[key].size(1) # get middle dim
    raise MotionCompatibilityError(f"No pos_encoder.positional_encoding found in mm_state_dict - {mm_type} is not a valid HotShotXL motion module!")


def validate_hsxl_block_count(mm_state_dict: dict[str, Tensor], mm_type: str) -> None:
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
    if biggest_block != 2:
        raise MotionCompatibilityError(f"Expected biggest down_block to be 2, but was {biggest_block} - {mm_type} is not a valid HotShotXL motion module!")


def has_mid_block(mm_state_dict: dict[str, Tensor]):
    # check if keys contain mid_block (temporal)
    for key in mm_state_dict.keys():
        if key.startswith("mid_block.") and "temporal" in key:
            return True
    return False


#########################################################################################
# Explanation for future me and other developers:
# Goal of the Wrapper and HotShotXLMotionModule is to create a structure compatible with the motion module to be loaded.
# Names of nn.ModuleList objects match that of entries of the motion module
#########################################################################################


class HotShotXLMotionWrapper(GenericMotionWrapper):
    def __init__(self, mm_state_dict: dict[str, Tensor], mm_hash: str, mm_name: str="mm_sd_v15.ckpt", loras: list[MotionLoRAInfo]=None):
        super().__init__(mm_hash, mm_name, loras)
        self.down_blocks: Iterable[HotShotXLMotionModule] = nn.ModuleList([])
        self.up_blocks: Iterable[HotShotXLMotionModule] = nn.ModuleList([])
        self.mid_block: Union[HotShotXLMotionModule, None] = None
        self.encoding_max_len = get_hsxl_temporal_position_encoding_max_len(mm_state_dict, mm_name)
        validate_hsxl_block_count(mm_state_dict, mm_name)
        for c in (320, 640, 1280):
            self.down_blocks.append(HotShotXLMotionModule(c, block_type=BlockType.DOWN, max_length=self.encoding_max_len))
        for c in (1280, 640, 320):
            self.up_blocks.append(HotShotXLMotionModule(c, block_type=BlockType.UP, max_length=self.encoding_max_len))
        if has_mid_block(mm_state_dict):
            self.mid_block = HotShotXLMotionModule(1280, BlockType=BlockType.MID, max_length=self.encoding_max_len)
        self.mm_hash = mm_hash
        self.mm_name = mm_name
        self.version = "HSXL v1" if self.mid_block is None else "HSXL v2"
        self.injector_version = InjectorVersion.HOTSHOTXL_V1
        self.AD_video_length: int = 8
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
        

class HotShotXLMotionModule(nn.Module):
    def __init__(self, in_channels, block_type: str=BlockType.DOWN, max_length=24):
        super().__init__()
        if block_type == BlockType.MID:
            # mid blocks contain only a single TransformerTemporal
            self.temporal_attentions: Iterable[TransformerTemporal] = nn.ModuleList([get_transformer_temporal(in_channels, max_length)])
        else:
            # down blocks contain two TransformerTemporals
            self.temporal_attentions = nn.ModuleList(
                [
                    get_transformer_temporal(in_channels, max_length),
                    get_transformer_temporal(in_channels, max_length)
                ]
            )
            # up blocks contain one additional TransformerTemporal
            if block_type == BlockType.UP:
                self.temporal_attentions.append(get_transformer_temporal(in_channels, max_length))

    def set_video_length(self, video_length: int, full_length: int):
        for tt in self.temporal_attentions:
            tt.set_video_length(video_length, full_length)

    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for tt in self.temporal_attentions:
            tt.set_scale_multiplier(multiplier)
    
    def set_masks(self, masks: Tensor, min_val: float, max_val: float):
        for tt in self.temporal_attentions:
            tt.set_masks(masks, min_val, max_val)
    
    def set_sub_idxs(self, sub_idxs: list[int]):
        for tt in self.temporal_attentions:
            tt.set_sub_idxs(sub_idxs)

    def reset_temp_vars(self):
        for tt in self.temporal_attentions:
            tt.reset_temp_vars()


def get_transformer_temporal(in_channels, max_length) -> 'TransformerTemporal':
    num_attention_heads = 8
    return TransformerTemporal(
        num_attention_heads=num_attention_heads,
        attention_head_dim=in_channels // num_attention_heads,
        in_channels=in_channels,
        max_length=max_length,
    )


class TransformerTemporal(nn.Module, TemporalTransformerGeneric):
    def __init__(
            self,
            num_attention_heads: int,
            attention_head_dim: int,
            in_channels: int,
            num_layers: int = 1,
            dropout: float = 0.0,
            norm_num_groups: int = 32,
            cross_attention_dim: Optional[int] = None,
            attention_bias: bool = False,
            activation_fn: str = "geglu",
            upcast_attention: bool = False,
            max_length = 24,
    ):
        super().__init__()
        super().temporal_transformer_init(default_length=8)

        inner_dim = num_attention_heads * attention_head_dim

        self.norm = GroupNormAD(num_groups=norm_num_groups, num_channels=in_channels, eps=1e-6, affine=True)
        self.proj_in = nn.Linear(in_channels, inner_dim)

        self.transformer_blocks: Iterable[TransformerBlock] = nn.ModuleList(
            [
                TransformerBlock(
                    dim=inner_dim,
                    num_attention_heads=num_attention_heads,
                    attention_head_dim=attention_head_dim,
                    dropout=dropout,
                    activation_fn=activation_fn,
                    attention_bias=attention_bias,
                    upcast_attention=upcast_attention,
                    cross_attention_dim=cross_attention_dim,
                    max_length=max_length,
                )
                for _ in range(num_layers)
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
                number_of_frames=self.video_length,
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


class TransformerBlock(nn.Module):
    def __init__(
            self,
            dim,
            num_attention_heads,
            attention_head_dim,
            dropout=0.0,
            activation_fn="geglu",
            attention_bias=False,
            upcast_attention=False,
            depth=2,
            cross_attention_dim: Optional[int] = None,
            max_length=24
    ):
        super().__init__()

        self.is_cross = cross_attention_dim is not None

        attention_blocks = []
        norms = []

        for _ in range(depth):
            attention_blocks.append(
                TemporalAttention(
                    max_length=max_length,
                    query_dim=dim,
                    context_dim=cross_attention_dim, # called context_dim for ComfyUI impl
                    heads=num_attention_heads,
                    dim_head=attention_head_dim,
                    dropout=dropout,
                    #bias=attention_bias, # remove for Comfy CrossAttention
                    #upcast_attention=upcast_attention, # remove for Comfy CrossAttention
                )
            )
            norms.append(nn.LayerNorm(dim))

        self.attention_blocks: Iterable[TemporalAttention] = nn.ModuleList(attention_blocks)
        self.norms = nn.ModuleList(norms)

        self.ff = FeedForward(dim, dropout=dropout, glu=(activation_fn == "geglu"))
        self.ff_norm = nn.LayerNorm(dim)

    def set_scale_multiplier(self, multiplier: Union[float, None]):
        for block in self.attention_blocks:
            block.set_scale_multiplier(multiplier)

    def forward(self, hidden_states, encoder_hidden_states=None, attention_mask=None, number_of_frames=None, scale_mask=None):
        if not self.is_cross:
            encoder_hidden_states = None

        for block, norm in zip(self.attention_blocks, self.norms):
            norm_hidden_states = norm(hidden_states)
            hidden_states = block(
                norm_hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                attention_mask=attention_mask,
                number_of_frames=number_of_frames,
                scale_mask=scale_mask
            ) + hidden_states

        norm_hidden_states = self.ff_norm(hidden_states)
        hidden_states = self.ff(norm_hidden_states) + hidden_states

        output = hidden_states
        return output


class PositionalEncoding(nn.Module):
    """
    Implements positional encoding as described in "Attention Is All You Need".
    Adds sinusoidal based positional encodings to the input tensor.
    """

    _SCALE_FACTOR = 10000.0  # Scale factor used in the positional encoding computation.

    def __init__(self, dim: int, dropout: float = 0.0, max_length: int = 24):
        super(PositionalEncoding, self).__init__()

        self.dropout = nn.Dropout(p=dropout)

        # The size is (1, max_length, dim) to allow easy addition to input tensors.
        positional_encoding = torch.zeros(1, max_length, dim)

        # Position and dim are used in the sinusoidal computation.
        position = torch.arange(max_length).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, dim, 2) * (-math.log(self._SCALE_FACTOR) / dim))

        positional_encoding[0, :, 0::2] = torch.sin(position * div_term)
        positional_encoding[0, :, 1::2] = torch.cos(position * div_term)

        # Register the positional encoding matrix as a buffer,
        # so it's part of the model's state but not the parameters.
        self.register_buffer('positional_encoding', positional_encoding)

    def forward(self, hidden_states: torch.Tensor, length: int) -> torch.Tensor:
        hidden_states = hidden_states + self.positional_encoding[:, :length]
        return self.dropout(hidden_states)


class TemporalAttention(CrossAttentionMM):
    def __init__(self, max_length=24, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pos_encoder = PositionalEncoding(kwargs["query_dim"], dropout=0, max_length=max_length)

    def set_scale_multiplier(self, multiplier: Union[float, None]):
        if multiplier is None or math.isclose(multiplier, 1.0):
            self.scale = None
        else:
            self.scale = multiplier

    def forward(self, hidden_states, encoder_hidden_states=None, attention_mask=None, number_of_frames=8, scale_mask=None):
        sequence_length = hidden_states.shape[1]
        hidden_states = rearrange(hidden_states, "(b f) s c -> (b s) f c", f=number_of_frames)
        hidden_states = self.pos_encoder(hidden_states, length=number_of_frames)

        if encoder_hidden_states:
            encoder_hidden_states = repeat(encoder_hidden_states, "b n c -> (b s) n c", s=sequence_length)

        hidden_states = super().forward(
            hidden_states,
            encoder_hidden_states,
            mask=attention_mask,
            scale_mask=scale_mask
        )

        return rearrange(hidden_states, "(b s) f c -> (b f) s c", s=sequence_length)
