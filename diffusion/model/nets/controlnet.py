from typing import Any, Iterator, Mapping, Union
from torch import Tensor
from torch.nn.modules.module import Module
from torch.nn.parameter import Parameter
from diffusion.model.nets import PixArtMSBlock, PixArtMS, PixArt
from torch.nn import Module, Linear, init
from copy import deepcopy
from diffusion.model.nets.PixArt import get_2d_sincos_pos_embed
import re
import torch
import torch.nn as nn
from diffusion.model.utils import auto_grad_checkpoint, to_2tuple
# Original Jincheng's architecture
class ControlT2IDitBlock(Module):
    def __init__(self, base_block: PixArtMSBlock) -> None:
        super().__init__()
        self.base_block = base_block
        self.copied_block = deepcopy(base_block)

        for p in self.copied_block.parameters():
            p.requires_grad_(True)

        self.copied_block.load_state_dict(base_block.state_dict())
        self.copied_block.train()
        self.hidden_size = hidden_size = base_block.hidden_size
        self.before_proj = Linear(hidden_size, hidden_size)
        self.after_proj = Linear(hidden_size, hidden_size)
        init.zeros_(self.before_proj.weight)
        init.zeros_(self.before_proj.bias)
        init.zeros_(self.after_proj.weight)
        init.zeros_(self.after_proj.bias)

    def forward(self, x, y, t, mask=None, c=None):
        if c is not None:
            c = self.before_proj(c)
            c = self.copied_block(x + c, y, t, mask)
            c = self.after_proj(c)
        with torch.no_grad():
            x = self.base_block(x, y, t, mask)
        return x + c if c is not None else x

class ControlT2IDiT(Module): #Jincheng's net
    def __init__(self, base_model: PixArtMS) -> None:
        super().__init__()
        base_model.eval()
        for p in base_model.parameters():
            p.requires_grad_(False)

        for i in range(len(base_model.blocks)):
            base_model.blocks[i] = ControlT2IDitBlock(base_model.blocks[i])

        self.base_model = base_model
    
    # def __getattr__(self, name: str) -> Tensor | Module:
    def __getattr__(self, name: str):
        if name in ['forward', 'forward_with_dpmsolver', 'forward_with_cfg', 'forward_c', 'load_state_dict']:
            return self.__dict__[name]
        elif name == 'base_model':
            return super().__getattr__(name)
        else:
            return getattr(self.base_model, name)

    def forward_c(self, c):
        self.h, self.w = c.shape[-2]//self.patch_size, c.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(c.device)
        return self.x_embedder(c) + pos_embed if c is not None else c

    def forward(self, x, t, c, **kwargs):
        return self.base_model(x, t, c=self.forward_c(c), **kwargs)

    def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
        return self.base_model.forward_with_dpmsolver(x, t, y, data_info=data_info, c=self.forward_c(c), **kwargs)

    def forward_with_cfg(self, x, t, y, cfg_scale, data_info, c, **kwargs):
        return self.base_model.forward_with_cfg(x, t, y, cfg_scale, data_info, c=self.forward_c(c), **kwargs)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        if all(k.startswith('base_model') or (k.startswith('controlnet')) for k in state_dict.keys()):
            return super().load_state_dict(state_dict, strict)
        else:
            new_key = {}
            for k in state_dict.keys():
                new_key[k] = re.sub(r"(blocks\.\d+)(.*)", r"\1.base_block\2", k)
            for k, v in new_key.items():
                if k != v:
                    print(f"replace {k} to {v}")
                    state_dict[v] = state_dict.pop(k)

            return self.base_model.load_state_dict(state_dict, strict)

# The implementation of ControlNet default
class ControlT2IDitBlockAll(Module):
    def __init__(self, base_block: PixArtMSBlock, block_index: 0) -> None:
        super().__init__()
        self.copied_block = deepcopy(base_block)
        self.block_index = block_index

        for p in self.copied_block.parameters():
            p.requires_grad_(True)

        self.copied_block.load_state_dict(base_block.state_dict())
        self.copied_block.train()
        
        self.hidden_size = hidden_size = base_block.hidden_size
        if self.block_index == 0:
            self.before_proj = Linear(hidden_size, hidden_size)
            init.zeros_(self.before_proj.weight)
            init.zeros_(self.before_proj.bias)
        self.after_proj = Linear(hidden_size, hidden_size) 
        init.zeros_(self.after_proj.weight)
        init.zeros_(self.after_proj.bias)

    def forward(self, x, y, t, mask=None, c=None):
        
        if self.block_index == 0:
            # the first block
            c = self.before_proj(c)
            c = self.copied_block(x + c, y, t, mask)
            c_skip = self.after_proj(c)
        else:
            # load from previous c and produce the c for skip connection
            c = self.copied_block(c, y, t, mask)
            c_skip = self.after_proj(c)
        
        return c, c_skip
        
        
        
        

# The implementation of controlnet-mid version
class ControlPixArtAll(Module): #add all the control
    def __init__(self, base_model: PixArtMS) -> None:
        super().__init__()
        self.base_model = base_model.eval()
        self.controlnet = []
        for p in self.base_model.parameters():
            p.requires_grad_(False)

        # Copy 0 - 13 block
        for i in range(14):
            self.controlnet.append(ControlT2IDitBlockAll(base_model.blocks[i], i))
        self.controlnet = nn.ModuleList(self.controlnet)
    
    # def __getattr__(self, name: str) -> Tensor | Module:
    def __getattr__(self, name: str):
        if name in ['forward', 'forward_with_dpmsolver', 'forward_with_cfg', 'forward_c', 'load_state_dict']:
            return self.__dict__[name]
        elif name in ['base_model', 'controlnet']:
            return super().__getattr__(name)
        else:
            return getattr(self.base_model, name)

    def forward_c(self, c):
        self.h, self.w = c.shape[-2]//self.patch_size, c.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(c.device)
        return self.x_embedder(c) + pos_embed if c is not None else c

    # def forward(self, x, t, c, **kwargs):
    #     return self.base_model(x, t, c=self.forward_c(c), **kwargs)
    def forward(self, x, t, y, mask=None, data_info=None, c=None, **kwargs):
        # modify the original pixartms forward function
        """
        Forward pass of PixArt.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N, 1, 120, C) tensor of class labels
        """
        if c is not None:
            c = self.forward_c(c)
        bs = x.shape[0]
        c_size, ar = data_info['img_hw'], data_info['aspect_ratio']
        self.h, self.w = x.shape[-2]//self.patch_size, x.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(x.device)
        x = self.x_embedder(x) + pos_embed  # (N, T, D), where T = H * W / patch_size ** 2
        t = self.t_embedder(t)  # (N, D)
        csize = self.csize_embedder(c_size, bs)  # (N, D)
        ar = self.ar_embedder(ar, bs)  # (N, D)
        t = t + torch.cat([csize, ar], dim=1)
        t0 = self.t_block(t)
        y = self.y_embedder(y, self.training)  # (N, D)
        if mask is not None:
            if mask.shape[0] != y.shape[0]:
                mask = mask.repeat(y.shape[0] // mask.shape[0], 1)
            mask = mask.squeeze(1).squeeze(1)
            y = y.squeeze(1).masked_select(mask.unsqueeze(-1) != 0).view(1, -1, x.shape[-1])
            y_lens = mask.sum(dim=1).tolist()
        else:
            y_lens = [y.shape[2]] * y.shape[0]
            y = y.squeeze(1).view(1, -1, x.shape[-1])
        
        # update x
        for index in range(14):
            x = auto_grad_checkpoint(self.base_model.blocks[index], x, y, t0, y_lens, **kwargs)  # (N, T, D) #support grad checkpoint

        # update c
        if c is not None:
            skip_c_list = []
            for index in range(14):
                c, c_skip = auto_grad_checkpoint(self.controlnet[index], x, y, t0, y_lens, c, **kwargs)
                skip_c_list.append(c_skip.clone())
        
            # update x
            for index in range(14, 28):
                x = auto_grad_checkpoint(self.base_model.blocks[index], x + skip_c_list[27 - index], y, t0, y_lens, **kwargs)
        else:
            for index in range(14, 28):
                x = auto_grad_checkpoint(self.base_model.blocks[index], x, y, t0, y_lens, **kwargs)

        x = self.final_layer(x, t)  # (N, T, patch_size ** 2 * out_channels)
        x = self.unpatchify(x)  # (N, out_channels, H, W)
        return x

    def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
        """
        dpm solver donnot need variance prediction
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        model_out = self.forward(x, t, y, data_info=data_info, c=c, **kwargs)
        return model_out.chunk(2, dim=1)[0]

    # def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
    #     return self.base_model.forward_with_dpmsolver(x, t, y, data_info=data_info, c=self.forward_c(c), **kwargs)

    def forward_with_cfg(self, x, t, y, cfg_scale, data_info, c, **kwargs):
        return self.base_model.forward_with_cfg(x, t, y, cfg_scale, data_info, c=self.forward_c(c), **kwargs)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        if all((k.startswith('base_model') or k.startswith('controlnet')) for k in state_dict.keys()):
            return super().load_state_dict(state_dict, strict)
        else:
            new_key = {}
            for k in state_dict.keys():
                new_key[k] = re.sub(r"(blocks\.\d+)(.*)", r"\1.base_block\2", k)
            for k, v in new_key.items():
                if k != v:
                    print(f"replace {k} to {v}")
                    state_dict[v] = state_dict.pop(k)

            return self.base_model.load_state_dict(state_dict, strict)
    
    def unpatchify(self, x):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.out_channels
        p = self.x_embedder.patch_size[0]
        assert self.h * self.w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], self.h, self.w, p, p, c))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, self.h * p, self.w * p))
        return imgs


# The implementation of ControlNet-Half architrecture
# https://github.com/lllyasviel/ControlNet/discussions/188
class ControlT2IDitBlockHalf(Module):
    def __init__(self, base_block: PixArtMSBlock, block_index: 0) -> None:
        super().__init__()
        self.copied_block = deepcopy(base_block)
        self.block_index = block_index

        for p in self.copied_block.parameters():
            p.requires_grad_(True)

        self.copied_block.load_state_dict(base_block.state_dict())
        self.copied_block.train()
        
        self.hidden_size = hidden_size = base_block.hidden_size
        if self.block_index == 0:
            self.before_proj = Linear(hidden_size, hidden_size)
            init.zeros_(self.before_proj.weight)
            init.zeros_(self.before_proj.bias)
        self.after_proj = Linear(hidden_size, hidden_size) 
        init.zeros_(self.after_proj.weight)
        init.zeros_(self.after_proj.bias)

    def forward(self, x, y, t, mask=None, c=None):
        
        if self.block_index == 0:
            # the first block
            c = self.before_proj(c)
            c = self.copied_block(x + c, y, t, mask)
            c_skip = self.after_proj(c)
        else:
            # load from previous c and produce the c for skip connection
            c = self.copied_block(c, y, t, mask)
            c_skip = self.after_proj(c)
        
        return c, c_skip
        
        
        
# The implementation of contropixhalf net
class ControlPixArtHalf(Module):
    # only support 512 res
    def __init__(self, base_model: PixArt) -> None:
        super().__init__()
        self.base_model = base_model.eval()
        self.controlnet = []
        for p in self.base_model.parameters():
            p.requires_grad_(False)

        # Copy 0 - 13 block
        for i in range(13):
            self.controlnet.append(ControlT2IDitBlockHalf(base_model.blocks[i], i))
        self.controlnet = nn.ModuleList(self.controlnet)
    
    # def __getattr__(self, name: str) -> Tensor | Module:
    def __getattr__(self, name: str):
        if name in ['forward', 'forward_with_dpmsolver', 'forward_with_cfg', 'forward_c', 'load_state_dict']:
            return self.__dict__[name]
        elif name in ['base_model', 'controlnet']:
            return super().__getattr__(name)
        else:
            return getattr(self.base_model, name)

    def forward_c(self, c):
        self.h, self.w = c.shape[-2]//self.patch_size, c.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(c.device)
        return self.x_embedder(c) + pos_embed if c is not None else c

    # def forward(self, x, t, c, **kwargs):
    #     return self.base_model(x, t, c=self.forward_c(c), **kwargs)
    def forward(self, x, t, y, mask=None, data_info=None, c=None, **kwargs):
        # modify the original pixartms forward function
        if c is not None:
            c = self.forward_c(c)
        bs = x.shape[0]
        c_size, ar = data_info['img_hw'], data_info['aspect_ratio']
        """
        Forward pass of PixArt.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N, 1, 120, C) tensor of class labels
        """
        self.h, self.w = x.shape[-2]//self.patch_size, x.shape[-1]//self.patch_size
        x = self.x_embedder(x) + self.pos_embed  # (N, T, D), where T = H * W / patch_size ** 2
        t = self.t_embedder(t)  # (N, D)
        t0 = self.t_block(t)
        y = self.y_embedder(y, self.training)  # (N, 1, L, D)
        if mask is not None:
            if mask.shape[0] != y.shape[0]:
                mask = mask.repeat(y.shape[0] // mask.shape[0], 1)
            mask = mask.squeeze(1).squeeze(1)
            y = y.squeeze(1).masked_select(mask.unsqueeze(-1) != 0).view(1, -1, x.shape[-1])
            y_lens = mask.sum(dim=1).tolist()
        else:
            y_lens = [y.shape[2]] * y.shape[0]
            y = y.squeeze(1).view(1, -1, x.shape[-1])

        # define the first layer
        x = auto_grad_checkpoint(self.base_model.blocks[0], x, y, t0, y_lens, **kwargs)  # (N, T, D) #support grad checkpoint

        if c is not None:
            # update c
            skip_c_list = []
            for index in range(1, 14):
                c, c_skip = auto_grad_checkpoint(self.controlnet[index - 1], x, y, t0, y_lens, c, **kwargs)
                x = auto_grad_checkpoint(self.base_model.blocks[index], x + c_skip, y, t0, y_lens, **kwargs)
        
            # update x
            for index in range(14, 28):
                x = auto_grad_checkpoint(self.base_model.blocks[index], x, y, t0, y_lens, **kwargs)
        else:
            for index in range(1, 28):
                x = auto_grad_checkpoint(self.base_model.blocks[index], x, y, t0, y_lens, **kwargs)

        x = self.final_layer(x, t)  # (N, T, patch_size ** 2 * out_channels)
        x = self.unpatchify(x)  # (N, out_channels, H, W)
        return x

    def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
        """
        dpm solver donnot need variance prediction
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        model_out = self.forward(x, t, y, data_info=data_info, c=c, **kwargs)
        return model_out.chunk(2, dim=1)[0]

    # def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
    #     return self.base_model.forward_with_dpmsolver(x, t, y, data_info=data_info, c=self.forward_c(c), **kwargs)

    def forward_with_cfg(self, x, t, y, cfg_scale, data_info, c, **kwargs):
        return self.base_model.forward_with_cfg(x, t, y, cfg_scale, data_info, c=self.forward_c(c), **kwargs)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        if all((k.startswith('base_model') or k.startswith('controlnet')) for k in state_dict.keys()):
            return super().load_state_dict(state_dict, strict)
        else:
            new_key = {}
            for k in state_dict.keys():
                new_key[k] = re.sub(r"(blocks\.\d+)(.*)", r"\1.base_block\2", k)
            for k, v in new_key.items():
                if k != v:
                    print(f"replace {k} to {v}")
                    state_dict[v] = state_dict.pop(k)

            return self.base_model.load_state_dict(state_dict, strict)
    
    def unpatchify(self, x):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.out_channels
        p = self.x_embedder.patch_size[0]
        assert self.h * self.w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], self.h, self.w, p, p, c))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, self.h * p, self.w * p))
        return imgs


class ControlT2IDitBlock_Mid(Module):
    def __init__(self, base_block: PixArtMSBlock) -> None:
        super().__init__()
        self.base_block = base_block
        self.copied_block = deepcopy(base_block)

        for p in self.copied_block.parameters():
            p.requires_grad_(True)

        self.copied_block.load_state_dict(base_block.state_dict())
        self.copied_block.train()
        self.hidden_size = hidden_size = base_block.hidden_size
        self.before_proj = Linear(hidden_size, hidden_size)
        self.after_proj = Linear(hidden_size, hidden_size)
        init.zeros_(self.before_proj.weight)
        init.zeros_(self.before_proj.bias)
        init.zeros_(self.after_proj.weight)
        init.zeros_(self.after_proj.bias)

    def forward(self, x, y, t, mask=None, c=None, box_number = 0):
        # Add control at block 13 and 14

        x = self.base_block(x, y, t, mask)
        
        if box_number == 0:
            # the first block
            c = self.before_proj(c)
            c = self.copied_block(x + c, y, t, mask)
            
        elif box_number < 13 and box_number > 0:
            c = self.copied_block(c, y, t, mask)
        elif box_number == 13:
            c = self.copied_block(c, y, t, mask)
            c = self.after_proj(c)
            x = x + c
        else:
            c = None
        
        return x, c
        
        
        
        

# The implementation of controlnet-mid version
class ControlPixArt_Mid(Module): #Only add mid control
    def __init__(self, base_model: PixArtMS) -> None:
        super().__init__()
        base_model.eval()
        for p in base_model.parameters():
            p.requires_grad_(False)

        for i in range(len(base_model.blocks)):
            base_model.blocks[i] = ControlT2IDitBlock_Mid(base_model.blocks[i])

        self.base_model = base_model
    
    # def __getattr__(self, name: str) -> Tensor | Module:
    def __getattr__(self, name: str):
        if name in ['forward', 'forward_with_dpmsolver', 'forward_with_cfg', 'forward_c', 'load_state_dict']:
            return self.__dict__[name]
        elif name == 'base_model':
            return super().__getattr__(name)
        else:
            return getattr(self.base_model, name)

    def forward_c(self, c):
        self.h, self.w = c.shape[-2]//self.patch_size, c.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(c.device)
        return self.x_embedder(c) + pos_embed if c is not None else c

    # def forward(self, x, t, c, **kwargs):
    #     return self.base_model(x, t, c=self.forward_c(c), **kwargs)
    def forward(self, x, t, y, mask=None, data_info=None, c=None, **kwargs):
        # modify the original pixartms forward function
        """
        Forward pass of PixArt.
        x: (N, C, H, W) tensor of spatial inputs (images or latent representations of images)
        t: (N,) tensor of diffusion timesteps
        y: (N, 1, 120, C) tensor of class labels
        """
        c = self.forward_c(c)
        bs = x.shape[0]
        c_size, ar = data_info['img_hw'], data_info['aspect_ratio']
        self.h, self.w = x.shape[-2]//self.patch_size, x.shape[-1]//self.patch_size
        pos_embed = torch.from_numpy(get_2d_sincos_pos_embed(self.pos_embed.shape[-1], (self.h, self.w), lewei_scale=self.lewei_scale, base_size=self.base_size)).float().unsqueeze(0).to(x.device)
        x = self.x_embedder(x) + pos_embed  # (N, T, D), where T = H * W / patch_size ** 2
        t = self.t_embedder(t)  # (N, D)
        csize = self.csize_embedder(c_size, bs)  # (N, D)
        ar = self.ar_embedder(ar, bs)  # (N, D)
        t = t + torch.cat([csize, ar], dim=1)
        t0 = self.t_block(t)
        y = self.y_embedder(y, self.training)  # (N, D)
        if mask is not None:
            if mask.shape[0] != y.shape[0]:
                mask = mask.repeat(y.shape[0] // mask.shape[0], 1)
            mask = mask.squeeze(1).squeeze(1)
            y = y.squeeze(1).masked_select(mask.unsqueeze(-1) != 0).view(1, -1, x.shape[-1])
            y_lens = mask.sum(dim=1).tolist()
        else:
            y_lens = [y.shape[2]] * y.shape[0]
            y = y.squeeze(1).view(1, -1, x.shape[-1])
        
        for index in range(len(self.blocks)):
            block = self.blocks[index]
            x, c = auto_grad_checkpoint(block, x, y, t0, y_lens, c, index, **kwargs)  # (N, T, D) #support grad checkpoint

        x = self.final_layer(x, t)  # (N, T, patch_size ** 2 * out_channels)
        x = self.unpatchify(x)  # (N, out_channels, H, W)
        return x

    def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
        """
        dpm solver donnot need variance prediction
        """
        # https://github.com/openai/glide-text2im/blob/main/notebooks/text2im.ipynb
        model_out = self.forward(x, t, y, data_info=data_info, c=c, **kwargs)
        return model_out.chunk(2, dim=1)[0]

    # def forward_with_dpmsolver(self, x, t, y, data_info, c, **kwargs):
    #     return self.base_model.forward_with_dpmsolver(x, t, y, data_info=data_info, c=self.forward_c(c), **kwargs)

    def forward_with_cfg(self, x, t, y, cfg_scale, data_info, c, **kwargs):
        return self.base_model.forward_with_cfg(x, t, y, cfg_scale, data_info, c=self.forward_c(c), **kwargs)

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        if all(k.startswith('base_model') for k in state_dict.keys()):
            return super().load_state_dict(state_dict, strict)
        else:
            new_key = {}
            for k in state_dict.keys():
                new_key[k] = re.sub(r"(blocks\.\d+)(.*)", r"\1.base_block\2", k)
            for k, v in new_key.items():
                if k != v:
                    print(f"replace {k} to {v}")
                    state_dict[v] = state_dict.pop(k)

            return self.base_model.load_state_dict(state_dict, strict)
    
    def unpatchify(self, x):
        """
        x: (N, T, patch_size**2 * C)
        imgs: (N, H, W, C)
        """
        c = self.out_channels
        p = self.x_embedder.patch_size[0]
        assert self.h * self.w == x.shape[1]

        x = x.reshape(shape=(x.shape[0], self.h, self.w, p, p, c))
        x = torch.einsum('nhwpqc->nchpwq', x)
        imgs = x.reshape(shape=(x.shape[0], c, self.h * p, self.w * p))
        return imgs