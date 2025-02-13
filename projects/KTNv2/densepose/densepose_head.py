# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import math
import pickle
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Tuple
import fvcore.nn.weight_init as weight_init
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.parameter import Parameter

from detectron2.config import CfgNode
from detectron2.layers import Conv2d, ConvTranspose2d, interpolate
from detectron2.structures import Instances
from detectron2.structures.boxes import matched_boxlist_iou
from detectron2.utils.registry import Registry

from .data.structures import DensePoseOutput

import json
import numpy as np

ROI_DENSEPOSE_HEAD_REGISTRY = Registry("ROI_DENSEPOSE_HEAD")
BLOBK_NUM = 5


class DensePoseUVConfidenceType(Enum):
    """
    Statistical model type for confidence learning, possible values:
     - "iid_iso": statistically independent identically distributed residuals
         with anisotropic covariance
     - "indep_aniso": statistically independent residuals with anisotropic
         covariances
    For details, see:
    N. Neverova, D. Novotny, A. Vedaldi "Correlated Uncertainty for Learning
    Dense Correspondences from Noisy Labels", p. 918--926, in Proc. NIPS 2019
    """

    # fmt: off
    IID_ISO     = "iid_iso"
    INDEP_ANISO = "indep_aniso"
    # fmt: on


@dataclass
class DensePoseUVConfidenceConfig:
    """
    Configuration options for confidence on UV data
    """

    enabled: bool = False
    # lower bound on UV confidences
    epsilon: float = 0.01
    type: DensePoseUVConfidenceType = DensePoseUVConfidenceType.IID_ISO


@dataclass
class DensePoseSegmConfidenceConfig:
    """
    Configuration options for confidence on segmentation
    """

    enabled: bool = False
    # lower bound on confidence values
    epsilon: float = 0.01


@dataclass
class DensePoseConfidenceModelConfig:
    """
    Configuration options for confidence models
    """

    # confidence for U and V values
    uv_confidence: DensePoseUVConfidenceConfig
    # segmentation confidence
    segm_confidence: DensePoseSegmConfidenceConfig

    @staticmethod
    def from_cfg(cfg: CfgNode) -> "DensePoseConfidenceModelConfig":
        return DensePoseConfidenceModelConfig(
            uv_confidence=DensePoseUVConfidenceConfig(
                enabled=cfg.MODEL.ROI_DENSEPOSE_HEAD.UV_CONFIDENCE.ENABLED,
                epsilon=cfg.MODEL.ROI_DENSEPOSE_HEAD.UV_CONFIDENCE.EPSILON,
                type=DensePoseUVConfidenceType(cfg.MODEL.ROI_DENSEPOSE_HEAD.UV_CONFIDENCE.TYPE),
            ),
            segm_confidence=DensePoseSegmConfidenceConfig(
                enabled=cfg.MODEL.ROI_DENSEPOSE_HEAD.SEGM_CONFIDENCE.ENABLED,
                epsilon=cfg.MODEL.ROI_DENSEPOSE_HEAD.SEGM_CONFIDENCE.EPSILON,
            ),
        )


def initialize_module_params(module):
    for name, param in module.named_parameters():
        if 'deconv_p' in name and "norm" in name:
            continue
        if 'ASPP' in name and "norm" in name:
            continue
        if 'dp_sem_head' in name and "norm" in name:
            continue
        if 'body_kpt' in name or "dp_emb_layer" in name or "kpt_surface_transfer_matrix" in name:
            continue
        if "bias" in name:
            nn.init.constant_(param, 0)
        elif "weight" in name:
            if len(param.size())<2:
                continue
            nn.init.kaiming_normal_(param, mode="fan_out", nonlinearity="relu")
        elif "body_mask" in name or "body_part" in name or "bbox_surface_transfer_matrix" in name \
                or "part_surface_transfer_matrix" in name:
            # print("init ",name)
            nn.init.normal_(param, std=0.001)


@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseDeepLabHead(nn.Module):
    def __init__(self, cfg, input_channels):
        super(DensePoseDeepLabHead, self).__init__()
        # fmt: off
        hidden_dim           = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_DIM
        kernel_size          = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_KERNEL
        norm                 = cfg.MODEL.ROI_DENSEPOSE_HEAD.DEEPLAB.NORM
        self.n_stacked_convs = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_STACKED_CONVS
        self.use_nonlocal    = cfg.MODEL.ROI_DENSEPOSE_HEAD.DEEPLAB.NONLOCAL_ON
        # fmt: on
        pad_size = kernel_size // 2
        n_channels = input_channels

        self.ASPP = ASPP(input_channels, [6, 12, 56], n_channels)  # 6, 12, 56
        self.add_module("ASPP", self.ASPP)

        if self.use_nonlocal:
            self.NLBlock = NONLocalBlock2D(input_channels, bn_layer=True)
            self.add_module("NLBlock", self.NLBlock)
        # weight_init.c2_msra_fill(self.ASPP)

        for i in range(self.n_stacked_convs):
            norm_module = nn.GroupNorm(32, hidden_dim) if norm == "GN" else None
            layer = Conv2d(
                n_channels,
                hidden_dim,
                kernel_size,
                stride=1,
                padding=pad_size,
                bias=not norm,
                norm=norm_module,
            )
            weight_init.c2_msra_fill(layer)
            n_channels = hidden_dim
            layer_name = self._get_layer_name(i)
            self.add_module(layer_name, layer)
        self.n_out_channels = hidden_dim
        # initialize_module_params(self)

    def forward(self, features):
        x0 = features
        x = self.ASPP(x0)
        if self.use_nonlocal:
            x = self.NLBlock(x)
        output = x
        for i in range(self.n_stacked_convs):
            layer_name = self._get_layer_name(i)
            x = getattr(self, layer_name)(x)
            x = F.relu(x)
            output = x
        return output

    def _get_layer_name(self, i: int):
        layer_name = "body_conv_fcn{}".format(i + 1)
        return layer_name

class ScaleConvs(nn.Module):
    def __init__(self, in_channels, atrous_rates, out_channels):
        super(ScaleConvs, self).__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 1, bias=False),
            nn.GroupNorm(32, out_channels),
            nn.ReLU(),
        )
        self.dilations = tuple(atrous_rates)
        self.paddings = [r for r in self.dilations]
        self.norms = nn.ModuleList([nn.GroupNorm(32, out_channels) for _ in self.dilations])
        self.weight = Parameter(
            torch.Tensor(out_channels, in_channels, 3, 3)
        )
        self.bias = None

        self.project = nn.Sequential(
            nn.Conv2d(4 * out_channels, out_channels, 1, bias=False),
            nn.ReLU()
        )
        print('init scale convs')
        initialize_module_params(self)

    def forward(self, x):
        # weight = self.weight.to(x.device)
        res = [
            nn.functional.conv2d(x, weight=self.weight, stride=1, padding=padding, dilation=dilation)
            for i, (dilation, padding) in enumerate(zip(self.dilations, self.paddings))
        ]
        for i in range(3):
            res[i] = F.relu(self.norms[i](res[i]))
        res.append(self.conv1(x))
        res = torch.cat(res, dim=1)
        return self.project(res)

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseKTNHead(nn.Module):
    def __init__(self, cfg, input_channels):
        super(DensePoseKTNHead, self).__init__()
        # fmt: off
        hidden_dim           = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_DIM
        kernel_size          = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_KERNEL
        norm                 = cfg.MODEL.ROI_DENSEPOSE_HEAD.DEEPLAB.NORM
        self.n_stacked_convs = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_STACKED_CONVS
        self.use_nonlocal    = cfg.MODEL.ROI_DENSEPOSE_HEAD.DEEPLAB.NONLOCAL_ON
        # fmt: on
        pad_size = kernel_size // 2
        n_channels = input_channels

        self.ScaleConvs = ScaleConvs(input_channels, [1, 6, 12], n_channels)
        self.add_module("ScaleConv", self.ScaleConvs)

        for i in range(self.n_stacked_convs):
            norm_module = nn.GroupNorm(32, hidden_dim) if norm == "GN" else None
            layer = Conv2d(
                n_channels,
                hidden_dim,
                kernel_size,
                stride=1,
                padding=pad_size,
                bias=not norm,
                norm=norm_module,
            )
            weight_init.c2_msra_fill(layer)
            n_channels = hidden_dim
            layer_name = self._get_layer_name(i)
            self.add_module(layer_name, layer)
        self.n_out_channels = hidden_dim

    def forward(self, features):
        x = self.ScaleConvs(features)
        output = x
        for i in range(self.n_stacked_convs):
            layer_name = self._get_layer_name(i)
            x = getattr(self, layer_name)(x)
            x = F.relu(x)
            output = x
        return output

    def _get_layer_name(self, i):
        layer_name = "body_conv_fcn{}".format(i + 1)
        return layer_name

# Copied from
# https://github.com/pytorch/vision/blob/master/torchvision/models/segmentation/deeplabv3.py
# See https://arxiv.org/pdf/1706.05587.pdf for details
class ASPPConv(nn.Sequential):
    def __init__(self, in_channels, out_channels, dilation):
        modules = [
            nn.Conv2d(
                in_channels, out_channels, 3, padding=dilation, dilation=dilation, bias=False
            ),
            nn.GroupNorm(32, out_channels),
            nn.ReLU(),
        ]
        super(ASPPConv, self).__init__(*modules)


class ASPPPooling(nn.Sequential):
    def __init__(self, in_channels, out_channels):
        super(ASPPPooling, self).__init__(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(in_channels, out_channels, 1, bias=False),
            nn.GroupNorm(32, out_channels),
            nn.ReLU(),
        )

    def forward(self, x):
        size = x.shape[-2:]
        x = super(ASPPPooling, self).forward(x)
        return F.interpolate(x, size=size, mode="bilinear", align_corners=False)


class ASPP(nn.Module):
    def __init__(self, in_channels, atrous_rates, out_channels):
        super(ASPP, self).__init__()
        modules = []
        modules.append(
            nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1, bias=False),
                nn.GroupNorm(32, out_channels),
                nn.ReLU(),
            )
        )

        rate1, rate2, rate3 = tuple(atrous_rates)
        modules.append(ASPPConv(in_channels, out_channels, rate1))
        modules.append(ASPPConv(in_channels, out_channels, rate2))
        modules.append(ASPPConv(in_channels, out_channels, rate3))
        modules.append(ASPPPooling(in_channels, out_channels))

        self.convs = nn.ModuleList(modules)

        self.project = nn.Sequential(
            nn.Conv2d(5 * out_channels, out_channels, 1, bias=False),
            # nn.BatchNorm2d(out_channels),
            nn.ReLU()
            # nn.Dropout(0.5)
        )

    def forward(self, x):
        res = []
        for conv in self.convs:
            res.append(conv(x))
        res = torch.cat(res, dim=1)
        return self.project(res)


# copied from
# https://github.com/AlexHex7/Non-local_pytorch/blob/master/lib/non_local_embedded_gaussian.py
# See https://arxiv.org/abs/1711.07971 for details
class _NonLocalBlockND(nn.Module):
    def __init__(
        self, in_channels, inter_channels=None, dimension=3, sub_sample=True, bn_layer=True
    ):
        super(_NonLocalBlockND, self).__init__()

        assert dimension in [1, 2, 3]

        self.dimension = dimension
        self.sub_sample = sub_sample

        self.in_channels = in_channels
        self.inter_channels = inter_channels

        if self.inter_channels is None:
            self.inter_channels = in_channels // 2
            if self.inter_channels == 0:
                self.inter_channels = 1

        if dimension == 3:
            conv_nd = nn.Conv3d
            max_pool_layer = nn.MaxPool3d(kernel_size=(1, 2, 2))
            bn = nn.GroupNorm  # (32, hidden_dim) #nn.BatchNorm3d
        elif dimension == 2:
            conv_nd = nn.Conv2d
            max_pool_layer = nn.MaxPool2d(kernel_size=(2, 2))
            bn = nn.GroupNorm  # (32, hidden_dim)nn.BatchNorm2d
        else:
            conv_nd = nn.Conv1d
            max_pool_layer = nn.MaxPool1d(kernel_size=2)
            bn = nn.GroupNorm  # (32, hidden_dim)nn.BatchNorm1d

        self.g = conv_nd(
            in_channels=self.in_channels,
            out_channels=self.inter_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )

        if bn_layer:
            self.W = nn.Sequential(
                conv_nd(
                    in_channels=self.inter_channels,
                    out_channels=self.in_channels,
                    kernel_size=1,
                    stride=1,
                    padding=0,
                ),
                bn(32, self.in_channels),
            )
            nn.init.constant_(self.W[1].weight, 0)
            nn.init.constant_(self.W[1].bias, 0)
        else:
            self.W = conv_nd(
                in_channels=self.inter_channels,
                out_channels=self.in_channels,
                kernel_size=1,
                stride=1,
                padding=0,
            )
            nn.init.constant_(self.W.weight, 0)
            nn.init.constant_(self.W.bias, 0)

        self.theta = conv_nd(
            in_channels=self.in_channels,
            out_channels=self.inter_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )
        self.phi = conv_nd(
            in_channels=self.in_channels,
            out_channels=self.inter_channels,
            kernel_size=1,
            stride=1,
            padding=0,
        )

        if sub_sample:
            self.g = nn.Sequential(self.g, max_pool_layer)
            self.phi = nn.Sequential(self.phi, max_pool_layer)

    def forward(self, x):
        """
        :param x: (b, c, t, h, w)
        :return:
        """

        batch_size = x.size(0)

        g_x = self.g(x).view(batch_size, self.inter_channels, -1)
        g_x = g_x.permute(0, 2, 1)

        theta_x = self.theta(x).view(batch_size, self.inter_channels, -1)
        theta_x = theta_x.permute(0, 2, 1)
        phi_x = self.phi(x).view(batch_size, self.inter_channels, -1)
        f = torch.matmul(theta_x, phi_x)
        f_div_C = F.softmax(f, dim=-1)

        y = torch.matmul(f_div_C, g_x)
        y = y.permute(0, 2, 1).contiguous()
        y = y.view(batch_size, self.inter_channels, *x.size()[2:])
        W_y = self.W(y)
        z = W_y + x

        return z


class NONLocalBlock2D(_NonLocalBlockND):
    def __init__(self, in_channels, inter_channels=None, sub_sample=True, bn_layer=True):
        super(NONLocalBlock2D, self).__init__(
            in_channels,
            inter_channels=inter_channels,
            dimension=2,
            sub_sample=sub_sample,
            bn_layer=bn_layer,
        )


@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseV1ConvXHead(nn.Module):
    def __init__(self, cfg, input_channels):
        super(DensePoseV1ConvXHead, self).__init__()
        # fmt: off
        hidden_dim           = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_DIM
        kernel_size          = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_KERNEL
        self.n_stacked_convs = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_STACKED_CONVS
        # fmt: on
        pad_size = kernel_size // 2
        n_channels = input_channels
        for i in range(self.n_stacked_convs):
            if i == self.n_stacked_convs-1:
                hidden_dim = 512
            layer = Conv2d(n_channels, hidden_dim, kernel_size, stride=1, padding=pad_size)
            layer_name = self._get_layer_name(i)
            self.add_module(layer_name, layer)
            n_channels = hidden_dim
        self.n_out_channels = n_channels
        initialize_module_params(self)

    def forward(self, features):
        x = features
        output = x
        for i in range(self.n_stacked_convs):
            layer_name = self._get_layer_name(i)
            x = getattr(self, layer_name)(x)
            x = F.relu(x)
            output = x
        return output

    def _get_layer_name(self, i):
        layer_name = "body_conv_fcn{}".format(i + 1)
        return layer_name

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePosRes2Head(nn.Module):
    def __init__(self, cfg, input_channels):
        super(DensePosRes2Head, self).__init__()
        # fmt: off
        hidden_dim           = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_DIM
        kernel_size          = cfg.MODEL.ROI_DENSEPOSE_HEAD.CONV_HEAD_KERNEL
        self.n_stacked_convs = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_STACKED_CONVS
        scale                = cfg.MODEL.ROI_DENSEPOSE_HEAD.RES2.SCALE
        use_GN               = cfg.MODEL.ROI_DENSEPOSE_HEAD.RES2.GN
        # fmt: on
        pad_size = kernel_size // 2
        n_channels = input_channels
        for i in range(self.n_stacked_convs):
            layer = Res2Conv(n_channels, scale, hidden_dim, kernel_size, pad_size, use_GN)
            layer_name = self._get_layer_name(i)
            self.add_module(layer_name, layer)
            n_channels = hidden_dim
        self.n_out_channels = n_channels

    def forward(self, features):
        x = features
        output = x
        for i in range(self.n_stacked_convs):
            layer_name = self._get_layer_name(i)
            x = getattr(self, layer_name)(x)
            x = F.relu(x)
            output = x
        return output

    def _get_layer_name(self, i):
        layer_name = "body_conv_fcn{}".format(i + 1)
        return layer_name

class Res2Conv(nn.Module):
    def __init__(self, in_channels, scale, out_channels, kernel_size, pad_size, use_GN=False):
        super(Res2Conv, self).__init__()
        self.width = out_channels // scale
        self.nums = scale - 1
        if in_channels != out_channels:
            self.conv1 = nn.Conv2d(in_channels, out_channels, 1, 1)
        else:
            self.conv1 = None
        modules = []
        for i in range(self.nums):
            modules.append(nn.Conv2d(self.width, self.width, kernel_size, 1, pad_size))
        self.convs = nn.ModuleList(modules)
        self.use_GN = use_GN
        if self.use_GN:
            self.GN = nn.GroupNorm(scale, out_channels, affine=True)
        self.fusion = nn.Conv2d(out_channels, out_channels, 1, 1) # conv 1*1

    def forward(self, x):
        res = []
        if self.conv1 is not None:
            x = self.conv1(x)
        spx = torch.split(x, self.width, dim=1)
        for i in range(self.nums):
            if i == 0:
                sp = spx[i]
            else:
                sp = spx[i] + sp
            sp = self.convs[i](sp)
            res.append(sp)
        res.append(spx[-1])
        res = torch.cat(res, dim=1)
        if self.use_GN:
            res = self.GN(res)

        res = self.fusion(res)

        return res

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePosePredictor(nn.Module):
    def __init__(self, cfg, input_channels):

        super(DensePosePredictor, self).__init__()
        dim_in = input_channels
        n_segm_chan = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.ann_index_lowres = ConvTranspose2d(
            dim_in, n_segm_chan, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.index_uv_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # self.u_lowres = ConvTranspose2d(
        #     dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        # )
        # self.v_lowres = ConvTranspose2d(
        #     dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        # )
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE
        self.confidence_model_cfg = DensePoseConfidenceModelConfig.from_cfg(cfg)
        self._initialize_confidence_estimation_layers(cfg, self.confidence_model_cfg, dim_in)
        initialize_module_params(self)

    def forward(self, head_outputs):
        ann_index_lowres = self.ann_index_lowres(head_outputs)
        index_uv_lowres = self.index_uv_lowres(head_outputs)
        u_lowres = self.u_lowres(head_outputs)
        v_lowres = self.v_lowres(head_outputs)

        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )

        ann_index = interp2d(ann_index_lowres)
        index_uv = interp2d(index_uv_lowres)
        u = interp2d(u_lowres)
        v = interp2d(v_lowres)
        (
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
            (ann_index, index_uv),
        ) = self._forward_confidence_estimation_layers(
            self.confidence_model_cfg, head_outputs, interp2d, ann_index, index_uv
        )
        return (
            (ann_index, index_uv, u, v),
            (ann_index_lowres, index_uv_lowres, u_lowres, v_lowres),
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
        )

    def _initialize_confidence_estimation_layers(
        self, cfg: CfgNode, confidence_model_cfg: DensePoseConfidenceModelConfig, dim_in: int
    ):
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        if confidence_model_cfg.uv_confidence.enabled:
            if confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.IID_ISO:
                self.sigma_2_lowres = ConvTranspose2d(
                    dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
                )
            elif confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.INDEP_ANISO:
                self.sigma_2_lowres = ConvTranspose2d(
                    dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
                )
                self.kappa_u_lowres = ConvTranspose2d(
                    dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
                )
                self.kappa_v_lowres = ConvTranspose2d(
                    dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
                )
            else:
                raise ValueError(
                    f"Unknown confidence model type: {confidence_model_cfg.confidence_model_type}"
                )
        if confidence_model_cfg.segm_confidence.enabled:
            self.fine_segm_confidence_lowres = ConvTranspose2d(
                dim_in, 1, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
            )
            self.coarse_segm_confidence_lowres = ConvTranspose2d(
                dim_in, 1, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
            )

    def _forward_confidence_estimation_layers(
        self, confidence_model_cfg, head_outputs, interp2d, ann_index, index_uv
    ):
        sigma_1, sigma_2, kappa_u, kappa_v = None, None, None, None
        sigma_1_lowres, sigma_2_lowres, kappa_u_lowres, kappa_v_lowres = None, None, None, None
        fine_segm_confidence_lowres, fine_segm_confidence = None, None
        coarse_segm_confidence_lowres, coarse_segm_confidence = None, None
        if confidence_model_cfg.uv_confidence.enabled:
            if confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.IID_ISO:
                sigma_2_lowres = self.sigma_2_lowres(head_outputs)
                sigma_2 = interp2d(sigma_2_lowres)
            elif confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.INDEP_ANISO:
                sigma_2_lowres = self.sigma_2_lowres(head_outputs)
                kappa_u_lowres = self.kappa_u_lowres(head_outputs)
                kappa_v_lowres = self.kappa_v_lowres(head_outputs)
                sigma_2 = interp2d(sigma_2_lowres)
                kappa_u = interp2d(kappa_u_lowres)
                kappa_v = interp2d(kappa_v_lowres)
            else:
                raise ValueError(
                    f"Unknown confidence model type: {confidence_model_cfg.confidence_model_type}"
                )
        if confidence_model_cfg.segm_confidence.enabled:
            fine_segm_confidence_lowres = self.fine_segm_confidence_lowres(head_outputs)
            fine_segm_confidence = interp2d(fine_segm_confidence_lowres)
            fine_segm_confidence = (
                F.softplus(fine_segm_confidence) + confidence_model_cfg.segm_confidence.epsilon
            )
            index_uv = index_uv * torch.repeat_interleave(
                fine_segm_confidence, index_uv.shape[1], dim=1
            )
            coarse_segm_confidence_lowres = self.coarse_segm_confidence_lowres(head_outputs)
            coarse_segm_confidence = interp2d(coarse_segm_confidence_lowres)
            coarse_segm_confidence = (
                F.softplus(coarse_segm_confidence) + confidence_model_cfg.segm_confidence.epsilon
            )
            ann_index = ann_index * torch.repeat_interleave(
                coarse_segm_confidence, ann_index.shape[1], dim=1
            )
        return (
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
            (ann_index, index_uv),
        )

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseKptRelationPredictor(DensePosePredictor):

    def __init__(self, cfg, input_channels):
        super(DensePoseKptRelationPredictor, self).__init__(cfg, input_channels)
        dim_in = input_channels
        self.dp_keypoints_on = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_ON
        self.KPT_UP_SCALE = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_UP_SCALE
        self.kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE

        if self.dp_keypoints_on:
            kpt_weight_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_CLASSIFIER_WEIGHT_DIR
            kpt_weight = pickle.load(open(kpt_weight_dir, 'rb'))
            np_kpt_weight = torch.FloatTensor(kpt_weight['kpt_weight'])
            np_kpt_bias = torch.FloatTensor(kpt_weight['kpt_bias'])
            self.body_kpt_weight = Parameter(data=np_kpt_weight, requires_grad=True)
            self.body_kpt_bias = Parameter(data=np_kpt_bias, requires_grad=True)
            sim_matrix_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_SURF_RELATION_DIR
            rel_matrix = pickle.load(open(sim_matrix_dir, 'rb'))
            rel_matrix = rel_matrix.transpose()
            rel_matrix = torch.FloatTensor(rel_matrix)
            self.kpt_surface_transfer_matrix = nn.Parameter(data=rel_matrix, requires_grad=True)
            index_weight_size = dim_in * self.kernel_size * self.kernel_size
            kpt_surface_transformer = []
            kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
            kpt_surface_transformer.append(nn.LeakyReLU(0.02))
            kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
            self.kpt_surface_transformer = nn.Sequential(*kpt_surface_transformer)

    def generate_surface_weights_from_kpt(self):
        kpt_weight = self.body_kpt_weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in*h*w))
        body_surface_weight = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_weight = self.kpt_surface_transformer(body_surface_weight)
        body_surface_weight = body_surface_weight.reshape((self.kpt_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def forward(self, head_outputs):
        ann_index_lowres = self.ann_index_lowres(head_outputs)
        u_lowres = self.u_lowres(head_outputs)
        v_lowres = self.v_lowres(head_outputs)
        k_lowres = nn.functional.conv_transpose2d(head_outputs, weight=self.body_kpt_weight, bias=self.body_kpt_bias,
                                 padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight = self.generate_surface_weights_from_kpt()
        index_uv_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight,
                                                          padding=int(self.kernel_size / 2 - 1), stride=2)
        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )

        ann_index = interp2d(ann_index_lowres)
        index_uv = interp2d(index_uv_lowres)
        u = interp2d(u_lowres)
        v = interp2d(v_lowres)
        if self.KPT_UP_SCALE > 2:
            k = interp2d(k_lowres)
        else:
            k = k_lowres
        (
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
            (ann_index, index_uv),
        ) = self._forward_confidence_estimation_layers(
            self.confidence_model_cfg, head_outputs, interp2d, ann_index, index_uv
        )
        return (
            (ann_index, index_uv, u, v, k),
            (ann_index_lowres, index_uv_lowres, u_lowres, v_lowres),
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
        )

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class SABLKptRelationPredictor(DensePosePredictor):

    def __init__(self, cfg, input_channels):
        super(SABLKptRelationPredictor, self).__init__(cfg, input_channels)
        dim_in = input_channels
        self.dp_keypoints_on = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_ON
        self.KPT_UP_SCALE = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_UP_SCALE
        self.kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE

        self.u_cls_lowres = ConvTranspose2d(
            dim_in, 24*BLOBK_NUM, self.kernel_size, stride=2, padding=int(self.kernel_size / 2 - 1)
        )
        self.v_cls_lowres = ConvTranspose2d(
            dim_in, 24*BLOBK_NUM, self.kernel_size, stride=2, padding=int(self.kernel_size / 2 - 1)
        )
        self.u_offset_lowres = ConvTranspose2d(
            dim_in, 24*BLOBK_NUM, self.kernel_size, stride=2, padding=int(self.kernel_size / 2 - 1)
        )
        self.v_offset_lowres = ConvTranspose2d(
            dim_in, 24*BLOBK_NUM, self.kernel_size, stride=2, padding=int(self.kernel_size / 2 - 1)
        )

        if self.dp_keypoints_on:
            kpt_weight_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_CLASSIFIER_WEIGHT_DIR
            kpt_weight = pickle.load(open(kpt_weight_dir, 'rb'))
            np_kpt_weight = torch.FloatTensor(kpt_weight['kpt_weight'])
            np_kpt_bias = torch.FloatTensor(kpt_weight['kpt_bias'])
            self.body_kpt_weight = Parameter(data=np_kpt_weight, requires_grad=True)
            self.body_kpt_bias = Parameter(data=np_kpt_bias, requires_grad=True)
            sim_matrix_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_SURF_RELATION_DIR
            rel_matrix = pickle.load(open(sim_matrix_dir, 'rb'))
            rel_matrix = rel_matrix.transpose()
            rel_matrix = torch.FloatTensor(rel_matrix)
            self.kpt_surface_transfer_matrix = nn.Parameter(data=rel_matrix, requires_grad=True)
            index_weight_size = dim_in * self.kernel_size * self.kernel_size
            kpt_surface_transformer = []
            kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
            kpt_surface_transformer.append(nn.LeakyReLU(0.02))
            kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
            self.kpt_surface_transformer = nn.Sequential(*kpt_surface_transformer)

    def generate_surface_weights_from_kpt(self):
        kpt_weight = self.body_kpt_weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in*h*w))
        body_surface_weight = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_weight = self.kpt_surface_transformer(body_surface_weight)
        body_surface_weight = body_surface_weight.reshape((self.kpt_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def forward(self, head_outputs):
        ann_index_lowres = self.ann_index_lowres(head_outputs)
        u_cls_lowres = self.u_cls_lowres(head_outputs)
        u_offset_lowres = self.u_offset_lowres(head_outputs)
        v_cls_lowres = self.v_cls_lowres(head_outputs)
        v_offset_lowres = self.v_offset_lowres(head_outputs)
        k_lowres = nn.functional.conv_transpose2d(head_outputs, weight=self.body_kpt_weight, bias=self.body_kpt_bias,
                                 padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight = self.generate_surface_weights_from_kpt()
        index_uv_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight,
                                                          padding=int(self.kernel_size / 2 - 1), stride=2)
        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )

        ann_index = interp2d(ann_index_lowres)
        index_uv = interp2d(index_uv_lowres)
        u_cls = interp2d(u_cls_lowres)
        u_offset = interp2d(u_offset_lowres)
        v_cls = interp2d(v_cls_lowres)
        v_offset = interp2d(v_offset_lowres)
        if self.KPT_UP_SCALE > 2:
            k = interp2d(k_lowres)
        else:
            k = k_lowres
        (
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
            (ann_index, index_uv),
        ) = self._forward_confidence_estimation_layers(
            self.confidence_model_cfg, head_outputs, interp2d, ann_index, index_uv
        )
        return (
            (ann_index, index_uv, u_cls, u_offset, v_cls, v_offset, k),
            (ann_index_lowres, index_uv_lowres, u_cls_lowres, u_offset_lowres, v_cls_lowres, v_offset_lowres),
            (sigma_1, sigma_2, kappa_u, kappa_v, fine_segm_confidence, coarse_segm_confidence),
            (
                sigma_1_lowres,
                sigma_2_lowres,
                kappa_u_lowres,
                kappa_v_lowres,
                fine_segm_confidence_lowres,
                coarse_segm_confidence_lowres,
            ),
        )

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseKTNv2Predictor(nn.Module):

    def __init__(self, cfg, input_channels):
        super(DensePoseKTNv2Predictor, self).__init__()
        dim_in = input_channels
        self.dp_keypoints_on = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_ON
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.KPT_UP_SCALE = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_UP_SCALE
        self.kernel_size = kernel_size
        self.i_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.u_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.v_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.m_lowres = ConvTranspose2d(
            dim_in, 2, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # KTM
        # keypoint transfer config
        kpt_weight_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_CLASSIFIER_WEIGHT_DIR
        kpt_weight = pickle.load(open(kpt_weight_dir, 'rb'))
        np_kpt_weight = torch.FloatTensor(kpt_weight['kpt_weight'])
        np_kpt_bias = torch.FloatTensor(kpt_weight['kpt_bias'])
        self.body_kpt_weight = Parameter(data=np_kpt_weight, requires_grad=True)
        self.body_kpt_bias = Parameter(data=np_kpt_bias, requires_grad=True)
        sim_matrix_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_SURF_RELATION_DIR
        rel_matrix = pickle.load(open(sim_matrix_dir, 'rb'))
        rel_matrix = rel_matrix.transpose()
        rel_matrix = torch.FloatTensor(rel_matrix)
        self.kpt_surface_transfer_matrix = nn.Parameter(data=rel_matrix, requires_grad=True)
        index_weight_size = dim_in * self.kernel_size * self.kernel_size
        kpt_surface_transformer = []
        kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        kpt_surface_transformer.append(nn.LeakyReLU(0.02))
        kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        self.kpt_surface_transformer = nn.Sequential(*kpt_surface_transformer)
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE
        # bbox transfer config
        self.bbox_surface_transfer_matrix = Parameter(torch.Tensor(dim_out_patches, 6))
        bbox_weight_size = cfg.MODEL.ROI_BOX_HEAD.FC_DIM
        bbox_surface_transformer = []
        bbox_surface_transformer.append(nn.Linear(bbox_weight_size+index_weight_size, index_weight_size))
        bbox_surface_transformer.append(nn.LeakyReLU(0.02))
        bbox_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        self.bbox_surface_transformer = nn.Sequential(*bbox_surface_transformer)
        initialize_module_params(self)

    def generate_weights(self):
        index_weight = self.index_uv_lowres.weight
        n_in, n_out, h, w = index_weight.size(0), index_weight.size(1), index_weight.size(2), index_weight.size(3)
        index_weight = torch.mean(index_weight,[2,3])
        index_weight = index_weight.permute((1, 0)).reshape((n_out, n_in))
        index_to_part_weight = self.part_weight_transformer(index_weight)
        index_to_body_weight = self.body_weight_transformer(index_weight)
        body_part_weight = torch.matmul(self.body_part_weight, index_to_part_weight)
        body_part_weight = body_part_weight.reshape((self.NUM_ANN_INDICES, n_in, h, w)).permute((1, 0, 2, 3))
        body_mask_weight = torch.matmul(self.body_mask_weight, index_to_body_weight)
        body_mask_weight = body_mask_weight.reshape((2, n_in, h, w)).permute((1, 0, 2, 3))
        return body_part_weight, body_mask_weight

    def generate_surface_weights_from_kpt(self):
        kpt_weight = self.body_kpt_weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in*h*w))
        body_surface_weight = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_weight = self.kpt_surface_transformer(body_surface_weight)
        body_surface_weight = body_surface_weight.reshape((self.kpt_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def cascade_generate_surface_weights(self, bbox_weight):
        kpt_weight = self.body_kpt_weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in * h * w))
        body_surface_param_from_kpt = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_param_from_box = torch.matmul(self.bbox_surface_transfer_matrix, bbox_weight)
        syn_body_surface_params = torch.cat([body_surface_param_from_kpt, body_surface_param_from_box], dim=1)

        body_surface_weight = self.bbox_surface_transformer(syn_body_surface_params)
        body_surface_weight = body_surface_weight.reshape((self.bbox_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def forward(self, head_outputs, bbox_params=None):
        ann_index_lowres = None #self.ann_index_lowres(head_outputs)
        i_lowres = self.i_lowres(head_outputs)
        u_lowres = self.u_lowres(head_outputs)
        v_lowres = self.v_lowres(head_outputs)
        m_lowres = self.m_lowres(head_outputs)
        k_lowres = nn.functional.conv_transpose2d(head_outputs, weight=self.body_kpt_weight, bias=self.body_kpt_bias,
                                 padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight_from_kpt = self.generate_surface_weights_from_kpt()
        index_uv_from_kpt_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight_from_kpt,
                                                          padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight_from_box = self.cascade_generate_surface_weights(bbox_params)
        index_uv_from_box_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight_from_box,
                                                                  padding=int(self.kernel_size / 2 - 1), stride=2)
        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )

        ann_index = None
        index_uv_from_kpt = interp2d(index_uv_from_kpt_lowres)
        index_uv_from_box = interp2d(index_uv_from_box_lowres)
        index_uv = interp2d(i_lowres)
        u = interp2d(u_lowres)
        v = interp2d(v_lowres)
        m = interp2d(m_lowres)
        if self.KPT_UP_SCALE > 2:
            k = interp2d(k_lowres)
        else:
            k = k_lowres
        # return (ann_index, index_uv, u, v, m, k), (ann_index, index_uv_from_kpt, u, v, m), (ann_index, index_uv_from_box, u, v, m)
        index_uv = (index_uv_from_kpt+index_uv_from_box)*0.5
        return (m, index_uv, u, v, m, k), (None, None, None, None, None, None), None

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseKTNv2PredictorV2(DensePosePredictor):
    NUM_ANN_INDICES = 15
    def __init__(self, cfg, input_channels):
        super(DensePoseKTNv2Predictor, self).__init__(cfg, input_channels)
        dim_in = input_channels
        dim_out_ann_index = self.NUM_ANN_INDICES
        self.dp_keypoints_on = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_ON
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.KPT_UP_SCALE = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_UP_SCALE
        self.kernel_size = kernel_size
        self.i_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.u_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.v_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.ann_index_lowres = ConvTranspose2d(
            dim_in, dim_out_ann_index, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.m_lowres = ConvTranspose2d(
            dim_in, 2, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # KTM
        # keypoint transfer config
        kpt_weight_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_CLASSIFIER_WEIGHT_DIR
        kpt_weight = pickle.load(open(kpt_weight_dir, 'rb'))
        np_kpt_weight = torch.FloatTensor(kpt_weight['kpt_weight'])
        np_kpt_bias = torch.FloatTensor(kpt_weight['kpt_bias'])
        self.body_kpt_weight = Parameter(data=np_kpt_weight, requires_grad=True)
        self.body_kpt_bias = Parameter(data=np_kpt_bias, requires_grad=True)
        sim_matrix_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_SURF_RELATION_DIR
        rel_matrix = pickle.load(open(sim_matrix_dir, 'rb'))
        rel_matrix = rel_matrix.transpose()
        rel_matrix = torch.FloatTensor(rel_matrix)
        self.kpt_surface_transfer_matrix = nn.Parameter(data=rel_matrix, requires_grad=True)
        index_weight_size = dim_in * self.kernel_size * self.kernel_size
        kpt_surface_transformer = []
        kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        kpt_surface_transformer.append(nn.LeakyReLU(0.02))
        kpt_surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        self.kpt_surface_transformer = nn.Sequential(*kpt_surface_transformer)
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE
        # bbox transfer config
        bbox_weight_size = cfg.MODEL.ROI_BOX_HEAD.FC_DIM
        self.bbox_surface_transfer_matrix = Parameter(torch.Tensor(dim_out_patches, 6))
        # part transfer config
        self.part_surface_transfer_matrix = Parameter(torch.Tensor(
            dim_out_patches, dim_out_ann_index))

        surface_transformer = []
        surface_transformer.append(nn.Linear(bbox_weight_size + index_weight_size + index_weight_size, index_weight_size))
        surface_transformer.append(nn.LeakyReLU(0.02))
        surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        self.parameter_transformer = nn.Sequential(*surface_transformer)

        initialize_module_params(self)

    def generate_weights(self):
        index_weight = self.index_uv_lowres.weight
        n_in, n_out, h, w = index_weight.size(0), index_weight.size(1), index_weight.size(2), index_weight.size(3)
        index_weight = torch.mean(index_weight,[2,3])
        index_weight = index_weight.permute((1, 0)).reshape((n_out, n_in))
        index_to_part_weight = self.part_weight_transformer(index_weight)
        index_to_body_weight = self.body_weight_transformer(index_weight)
        body_part_weight = torch.matmul(self.body_part_weight, index_to_part_weight)
        body_part_weight = body_part_weight.reshape((self.NUM_ANN_INDICES, n_in, h, w)).permute((1, 0, 2, 3))
        body_mask_weight = torch.matmul(self.body_mask_weight, index_to_body_weight)
        body_mask_weight = body_mask_weight.reshape((2, n_in, h, w)).permute((1, 0, 2, 3))
        return body_part_weight, body_mask_weight
    def generate_surface_weights_from_kpt(self):
        kpt_weight = self.body_kpt_weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in*h*w))
        body_surface_weight = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_weight = self.kpt_surface_transformer(body_surface_weight)
        body_surface_weight = body_surface_weight.reshape((self.kpt_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight
    def cascade_generate_surface_weights(self, bbox_weight):
        kpt_weight = self.body_kpt_weight
        part_weight = self.ann_index_lowres.weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in * h * w))
        n_in, n_out, h, w = part_weight.size()
        part_weight = part_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in * h * w))
        body_surface_param_from_kpt = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        body_surface_param_from_box = torch.matmul(self.bbox_surface_transfer_matrix, bbox_weight)
        body_surface_param_from_part = torch.matmul(self.part_surface_transfer_matrix, part_weight)
        syn_body_surface_params = torch.cat([body_surface_param_from_kpt, body_surface_param_from_box, body_surface_param_from_part], dim=1)

        body_surface_weight = self.parameter_transformer(syn_body_surface_params)
        body_surface_weight = body_surface_weight.reshape((self.bbox_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def forward(self, head_outputs, bbox_params=None):
        ann_index_lowres = self.ann_index_lowres(head_outputs)
        i_lowres = self.i_lowres(head_outputs)
        u_lowres = self.u_lowres(head_outputs)
        v_lowres = self.v_lowres(head_outputs)
        m_lowres = self.m_lowres(head_outputs)
        k_lowres = nn.functional.conv_transpose2d(head_outputs, weight=self.body_kpt_weight, bias=self.body_kpt_bias,
                                 padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight_from_kpt = self.generate_surface_weights_from_kpt()
        index_uv_from_kpt_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight_from_kpt,
                                                          padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight_from_all = self.cascade_generate_surface_weights(bbox_params)
        index_uv_from_all_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight_from_all,
                                                                  padding=int(self.kernel_size / 2 - 1), stride=2)
        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )

        ann_index = interp2d(ann_index_lowres)
        index_uv_from_kpt = interp2d(index_uv_from_kpt_lowres)
        index_uv_from_all_lowres = interp2d(index_uv_from_all_lowres)
        index_uv = interp2d(i_lowres)
        u = interp2d(u_lowres)
        v = interp2d(v_lowres)
        m = interp2d(m_lowres)
        if self.KPT_UP_SCALE > 2:
            k = interp2d(k_lowres)
        else:
            k = k_lowres
        return (
            (ann_index, index_uv, u, v, m, k),
            (ann_index, index_uv_from_kpt, u, v, m),
            (ann_index, index_uv_from_all_lowres, u, v, m)
        )

@ROI_DENSEPOSE_HEAD_REGISTRY.register()
class DensePoseKTNv2PredictorV3(nn.Module):
    NUM_ANN_INDICES = 15
    def __init__(self, cfg, input_channels):
        super(DensePoseKTNv2PredictorV3, self).__init__()
        dim_in = input_channels
        dim_out_ann_index = self.NUM_ANN_INDICES
        self.dp_keypoints_on = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_ON
        dim_out_patches = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_PATCHES + 1
        kernel_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.DECONV_KERNEL
        self.KPT_UP_SCALE = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_UP_SCALE
        self.scale_factor = cfg.MODEL.ROI_DENSEPOSE_HEAD.UP_SCALE
        self.kernel_size = kernel_size
        self.i_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.u_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.v_lowres = ConvTranspose2d(
            dim_in, dim_out_patches, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.ann_index_lowres = ConvTranspose2d(
            dim_in, dim_out_ann_index, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        self.m_lowres = ConvTranspose2d(
            dim_in, 2, kernel_size, stride=2, padding=int(kernel_size / 2 - 1)
        )
        # KTM
        # keypoint transfer config
        kpt_weight_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_CLASSIFIER_WEIGHT_DIR
        kpt_weight = pickle.load(open(kpt_weight_dir, 'rb'))
        np_kpt_weight = torch.FloatTensor(kpt_weight['kpt_weight'])
        np_kpt_bias = torch.FloatTensor(kpt_weight['kpt_bias'])
        self.body_kpt_weight = Parameter(data=np_kpt_weight, requires_grad=True)
        self.body_kpt_bias = Parameter(data=np_kpt_bias, requires_grad=True)
        sim_matrix_dir = cfg.MODEL.ROI_DENSEPOSE_HEAD.KPT_SURF_RELATION_DIR
        rel_matrix = pickle.load(open(sim_matrix_dir, 'rb'))
        rel_matrix = rel_matrix.transpose()
        rel_matrix = torch.FloatTensor(rel_matrix)
        self.kpt_surface_transfer_matrix = nn.Parameter(data=rel_matrix, requires_grad=True)
        index_weight_size = dim_in * self.kernel_size * self.kernel_size

        # # # bbox transfer config
        bbox_weight_size = cfg.MODEL.ROI_BOX_HEAD.FC_DIM
        self.bbox_surface_transfer_matrix = Parameter(torch.Tensor(dim_out_patches, 6))
        self.part_surface_transfer_matrix = Parameter(torch.Tensor(
            dim_out_patches, dim_out_ann_index))
        surface_transformer = []
        surface_transformer.append(nn.Linear(index_weight_size+index_weight_size+bbox_weight_size, index_weight_size))
        surface_transformer.append(nn.LeakyReLU(0.02))
        surface_transformer.append(nn.Linear(index_weight_size, index_weight_size))
        self.parameter_transformer = nn.Sequential(*surface_transformer)

        initialize_module_params(self)

    def cascade_generate_surface_weights(self, bbox_weight):
        kpt_weight = self.body_kpt_weight
        part_weight = self.ann_index_lowres.weight
        n_in, n_out, h, w = kpt_weight.size(0), kpt_weight.size(1), kpt_weight.size(2), kpt_weight.size(3)
        kpt_weight = kpt_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in * h * w))
        n_in, n_out, h, w = part_weight.size()
        part_weight = part_weight.permute((1, 0, 2, 3)).reshape((n_out, n_in * h * w))
        body_surface_param_from_box = torch.matmul(self.bbox_surface_transfer_matrix, bbox_weight)
        # body_surface_param_from_box = self.bbox_surface_transformer(body_surface_param_from_box)
        body_surface_param_from_kpt = torch.matmul(self.kpt_surface_transfer_matrix, kpt_weight)
        # body_surface_param_from_kpt = self.kpt_surface_transformer(body_surface_param_from_kpt)

        body_surface_param_from_part = torch.matmul(self.part_surface_transfer_matrix, part_weight)
        # body_surface_param_from_part = self.part_surface_transformer(body_surface_param_from_part)
        syn_body_surface_params = torch.cat([body_surface_param_from_kpt, body_surface_param_from_box, body_surface_param_from_part],dim=1)

        body_surface_weight = self.parameter_transformer(syn_body_surface_params)
        body_surface_weight = body_surface_weight.reshape((self.kpt_surface_transfer_matrix.size(0), n_in, h, w)).permute((1, 0, 2, 3))
        return body_surface_weight

    def forward(self, head_outputs, bbox_params=None):
        ann_index_lowres = self.ann_index_lowres(head_outputs)
        i_lowres = self.i_lowres(head_outputs)
        u_lowres = self.u_lowres(head_outputs)
        v_lowres = self.v_lowres(head_outputs)
        m_lowres = self.m_lowres(head_outputs)
        k_lowres = nn.functional.conv_transpose2d(head_outputs, weight=self.body_kpt_weight, bias=self.body_kpt_bias,
                                 padding=int(self.kernel_size / 2 - 1), stride=2)
        body_surface_weight_from_all = self.cascade_generate_surface_weights(bbox_params)
        index_uv_from_all_lowres = nn.functional.conv_transpose2d(head_outputs, weight=body_surface_weight_from_all,
                                                                  padding=int(self.kernel_size / 2 - 1), stride=2)
        def interp2d(input):
            return interpolate(
                input, scale_factor=self.scale_factor, mode="bilinear", align_corners=False
            )
        ann_index = interp2d(ann_index_lowres)
        index_uv_from_all_lowres = interp2d(index_uv_from_all_lowres)
        index_uv = interp2d(i_lowres)
        u = interp2d(u_lowres)
        v = interp2d(v_lowres)
        m = interp2d(m_lowres)
        if self.KPT_UP_SCALE > 2:
            k = interp2d(k_lowres)
        else:
            k = k_lowres
        # return (ann_index, index_uv, u, v, m, k), (ann_index, index_uv_from_all_lowres, u, v, m), None
        return (m, index_uv_from_all_lowres, u, v, ann_index, k), (ann_index, index_uv, u, v, m), None

def dp_keypoint_rcnn_loss(pred_keypoint_logits, instances, normalizer):

    heatmaps = []
    valid = []

    keypoint_side_len = pred_keypoint_logits.shape[2]
    for instances_per_image in instances:
        if len(instances_per_image) == 0:
            continue
        keypoints = instances_per_image.gt_keypoints
        heatmaps_per_image, valid_per_image = keypoints.to_heatmap(
            instances_per_image.proposal_boxes.tensor, keypoint_side_len
        )
        heatmaps.append(heatmaps_per_image.view(-1))
        valid.append(valid_per_image.view(-1))

    if len(heatmaps):
        keypoint_targets = torch.cat(heatmaps, dim=0)
        valid = torch.cat(valid, dim=0).to(dtype=torch.uint8)
        valid = torch.nonzero(valid).squeeze(1)

    # torch.mean (in binary_cross_entropy_with_logits) doesn't
    # accept empty tensors, so handle it separately
    if len(heatmaps) == 0 or valid.numel() == 0:

        return pred_keypoint_logits.sum() * 0

    N, K, H, W = pred_keypoint_logits.shape
    pred_keypoint_logits = pred_keypoint_logits.view(N * K, H * W)

    keypoint_loss = F.cross_entropy(
        pred_keypoint_logits[valid], keypoint_targets[valid], reduction="sum"
    )

    # If a normalizer isn't specified, normalize by the number of visible keypoints in the minibatch
    if normalizer is None:
        normalizer = valid.numel()
    keypoint_loss /= normalizer

    return keypoint_loss

class DensePoseDataFilter(object):
    def __init__(self, cfg):
        self.iou_threshold = cfg.MODEL.ROI_DENSEPOSE_HEAD.FG_IOU_THRESHOLD
        self.keep_masks = cfg.MODEL.ROI_DENSEPOSE_HEAD.COARSE_SEGM_TRAINED_BY_MASKS

    @torch.no_grad()
    def __call__(self, features: List[torch.Tensor], proposals_with_targets: List[Instances]):
        """
        Filters proposals with targets to keep only the ones relevant for
        DensePose training

        Args:
            features (list[Tensor]): input data as a list of features,
                each feature is a tensor. Axis 0 represents the number of
                images `N` in the input data; axes 1-3 are channels,
                height, and width, which may vary between features
                (e.g., if a feature pyramid is used).
            proposals_with_targets (list[Instances]): length `N` list of
                `Instances`. The i-th `Instances` contains instances
                (proposals, GT) for the i-th input image,
        """
        proposals_filtered = []
        # TODO: the commented out code was supposed to correctly deal with situations
        # where no valid DensePose GT is available for certain images. The corresponding
        # image features were sliced and proposals were filtered. This led to performance
        # deterioration, both in terms of runtime and in terms of evaluation results.
        #
        # feature_mask = torch.ones(
        #    len(proposals_with_targets),
        #    dtype=torch.bool,
        #    device=features[0].device if len(features) > 0 else torch.device("cpu"),
        # )
        for i, proposals_per_image in enumerate(proposals_with_targets):
            if not proposals_per_image.has("gt_densepose") and (
                not proposals_per_image.has("gt_masks") or not self.keep_masks
            ):
                # feature_mask[i] = 0
                continue
            gt_boxes = proposals_per_image.gt_boxes
            est_boxes = proposals_per_image.proposal_boxes
            # apply match threshold for densepose head
            iou = matched_boxlist_iou(gt_boxes, est_boxes)
            iou_select = iou > self.iou_threshold
            proposals_per_image = proposals_per_image[iou_select]

            N_gt_boxes = len(proposals_per_image.gt_boxes)
            assert N_gt_boxes == len(proposals_per_image.proposal_boxes), (
                f"The number of GT boxes {N_gt_boxes} is different from the "
                f"number of proposal boxes {len(proposals_per_image.proposal_boxes)}"
            )
            # filter out any target without suitable annotation
            if self.keep_masks:
                gt_masks = (
                    proposals_per_image.gt_masks
                    if hasattr(proposals_per_image, "gt_masks")
                    else [None] * N_gt_boxes
                )
            else:
                gt_masks = [None] * N_gt_boxes
            gt_densepose = (
                proposals_per_image.gt_densepose
                if hasattr(proposals_per_image, "gt_densepose")
                else [None] * N_gt_boxes
            )
            assert len(gt_masks) == N_gt_boxes
            assert len(gt_densepose) == N_gt_boxes
            selected_indices = [
                i
                for i, (dp_target, mask_target) in enumerate(zip(gt_densepose, gt_masks))
                if (dp_target is not None) or (mask_target is not None)
            ]
            # if not len(selected_indices):
            #     feature_mask[i] = 0
            #     continue
            if len(selected_indices) != N_gt_boxes:
                proposals_per_image = proposals_per_image[selected_indices]
            assert len(proposals_per_image.gt_boxes) == len(proposals_per_image.proposal_boxes)
            proposals_filtered.append(proposals_per_image)
        # features_filtered = [feature[feature_mask] for feature in features]
        # return features_filtered, proposals_filtered
        return features, proposals_filtered

def build_densepose_head(cfg, input_channels):
    head_name = cfg.MODEL.ROI_DENSEPOSE_HEAD.NAME
    return ROI_DENSEPOSE_HEAD_REGISTRY.get(head_name)(cfg, input_channels)

def build_densepose_predictor(cfg, input_channels):
    predictor_name = cfg.MODEL.ROI_DENSEPOSE_HEAD.PREDICTOR
    return ROI_DENSEPOSE_HEAD_REGISTRY.get(predictor_name)(cfg, input_channels)

def build_densepose_data_filter(cfg):
    dp_filter = DensePoseDataFilter(cfg)
    return dp_filter


def densepose_inference(
    densepose_outputs: Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor],
    densepose_confidences: Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor],
    detections: List[Instances],
    mask_thresh = 0.5
):
    """
    Infer dense pose estimate based on outputs from the DensePose head
    and detections. The estimate for each detection instance is stored in its
    "pred_densepose" attribute.

    Args:
        densepose_outputs (tuple(`torch.Tensor`)): iterable containing 4 elements:
            - s (:obj: `torch.Tensor`): coarse segmentation tensor of size (N, A, H, W),
            - i (:obj: `torch.Tensor`): fine segmentation tensor of size (N, C, H, W),
            - u (:obj: `torch.Tensor`): U coordinates for each class of size (N, C, H, W),
            - v (:obj: `torch.Tensor`): V coordinates for each class of size (N, C, H, W),
            where N is the total number of detections in a batch,
                  A is the number of coarse segmentations labels
                      (e.g. 15 for coarse body parts + background),
                  C is the number of fine segmentation labels
                      (e.g. 25 for fine body parts + background),
                  W is the resolution along the X axis
                  H is the resolution along the Y axis
        densepose_confidences (tuple(`torch.Tensor`)): iterable containing 4 elements:
            - sigma_1 (:obj: `torch.Tensor`): global confidences for UV coordinates
                of size (N, C, H, W)
            - sigma_2 (:obj: `torch.Tensor`): individual confidences for UV coordinates
                of size (N, C, H, W)
            - kappa_u (:obj: `torch.Tensor`): first component of confidence direction
                vector of size (N, C, H, W)
            - kappa_v (:obj: `torch.Tensor`): second component of confidence direction
                vector of size (N, C, H, W)
            - fine_segm_confidence (:obj: `torch.Tensor`): confidence for fine
                segmentation of size (N, 1, H, W)
            - coarse_segm_confidence (:obj: `torch.Tensor`): confidence for coarse
                segmentation of size (N, 1, H, W)
        detections (list[Instances]): A list of N Instances, where N is the number of images
            in the batch. Instances are modified by this method: "pred_densepose" attribute
            is added to each instance, the attribute contains the corresponding
            DensePoseOutput object.
    """
    # DensePose outputs: segmentation, body part indices, U, V
    s, index_uv, u, v = densepose_outputs
    (
        sigma_1,
        sigma_2,
        kappa_u,
        kappa_v,
        fine_segm_confidence,
        coarse_segm_confidence,
    ) = densepose_confidences
    k = 0
    for detection in detections:
        n_i = len(detection)
        s_i = s[k : k + n_i]
        index_uv_i = index_uv[k : k + n_i]
        u_i = u[k : k + n_i]
        v_i = v[k : k + n_i]
        _local_vars = locals()
        confidences = {
            name: _local_vars[name][k : k + n_i]
            for name in (
                "sigma_1",
                "sigma_2",
                "kappa_u",
                "kappa_v",
                "fine_segm_confidence",
                "coarse_segm_confidence",
            )
            if _local_vars.get(name) is not None
        }
        densepose_output_i = DensePoseOutput(s_i, index_uv_i, u_i, v_i, confidences, mask_thresh)
        detection.pred_densepose = densepose_output_i
        k += n_i

def sabl_inference(
    densepose_outputs: Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor],
    densepose_confidences: Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor],
    detections: List[Instances],
    mask_thresh = 0.5
):
    
    # DensePose outputs: segmentation, body part indices, U, V
    s, index_uv, u_cls, u_offset, v_cls, v_offset = densepose_outputs
    with open("/home/sunjunyao/detectron2/projects/PoiseNet/models/block-5.json", "rb") as bFile:
        block = json.loads(json.load(bFile))
    block_u_width = torch.tensor(np.array(block["bucket_u_width"]), dtype=torch.float32,device=u_cls.device)
    block_v_width =  torch.tensor(np.array(block["bucket_v_width"]), dtype=torch.float32,device=u_cls.device)
    block_u_center =  torch.tensor(np.array(block["bucket_u_center"]), dtype=torch.float32,device=u_cls.device)
    block_v_center =  torch.tensor(np.array(block["bucket_v_center"]), dtype=torch.float32,device=u_cls.device)
    del block
    
    u_cls = u_cls.reshape(u_cls.shape[0],24,BLOBK_NUM,u_cls.shape[2], u_cls.shape[3])
    u_offset = u_offset.reshape(u_offset.shape[0],24,BLOBK_NUM,u_offset.shape[2], u_offset.shape[3])
    v_cls = v_cls.reshape(v_cls.shape[0],24,BLOBK_NUM,v_cls.shape[2], v_cls.shape[3])
    v_offset = v_offset.reshape(v_offset.shape[0],24,BLOBK_NUM,v_offset.shape[2], v_offset.shape[3])
    block_u_width = block_u_width[None,:,:,None,None].repeat(1,1,1,u_cls.shape[3], u_cls.shape[4])
    block_v_width = block_v_width[None,:,:,None,None].repeat(1,1,1,u_cls.shape[3], u_cls.shape[4])
    block_u_center = block_u_center[None,:,:,None,None].repeat(1,1,1,u_cls.shape[3], u_cls.shape[4])
    block_v_center = block_v_center[None,:,:,None,None].repeat(1,1,1,u_cls.shape[3], u_cls.shape[4])
    (
        sigma_1,
        sigma_2,
        kappa_u,
        kappa_v,
        fine_segm_confidence,
        coarse_segm_confidence,
    ) = densepose_confidences
    k = 0
    for detection in detections:
        n_i = len(detection)
        # if n_i == 0:
        #     continue
        s_i = s[k : k + n_i]
        index_uv_i = index_uv[k : k + n_i]
        if n_i == 0:
            u_i = index_uv[k : k + n_i]
            v_i = index_uv[k : k + n_i]
        else:
            u_cls_i = u_cls[k : k + n_i]
            u_offset_i = u_offset[k : k + n_i]
            v_cls_i = v_cls[k : k + n_i]
            v_offset_i = v_offset[k : k + n_i]
            block_u_width = block_u_width.repeat(n_i,1,1,1,1)
            block_v_width = block_v_width.repeat(n_i,1,1,1,1)
            block_u_center = block_u_center.repeat(n_i,1,1,1,1)
            block_v_center = block_v_center.repeat(n_i,1,1,1,1)
        # if n_i == 1:
        #     u_cls_i = u_cls_i.unsqueeze(0)
        #     u_offset_i = u_offset_i.unsqueeze(0)
        #     v_cls_i = v_cls_i.unsqueeze(0)
            # v_offset_i = v_offset_i.unsqueeze(0)
            index_u = torch.argmax(u_cls_i, dim=2).unsqueeze(2) # n*24*1*h*w
            index_v = torch.argmax(v_cls_i, dim=2).unsqueeze(2)
            u_i = u_offset_i.gather(2, index_u).squeeze(2) * block_u_width.gather(2, index_u).squeeze(2) + block_u_center.gather(2, index_u).squeeze(2)
            v_i = v_offset_i.gather(2, index_v).squeeze(2) * block_v_width.gather(2, index_v).squeeze(2) + block_v_center.gather(2, index_v).squeeze(2)
            bg_par = torch.zeros((u_i.shape[0], 1, u_i.shape[2], u_i.shape[3]), dtype=torch.float32,device=u_cls_i.device)
            u_i = torch.cat((bg_par, u_i), dim=1)
            v_i = torch.cat((bg_par, v_i), dim=1)

        _local_vars = locals()
        confidences = {
            name: _local_vars[name][k : k + n_i]
            for name in (
                "sigma_1",
                "sigma_2",
                "kappa_u",
                "kappa_v",
                "fine_segm_confidence",
                "coarse_segm_confidence",
            )
            if _local_vars.get(name) is not None
        }
        densepose_output_i = DensePoseOutput(s_i, index_uv_i, u_i, v_i, confidences, mask_thresh)
        detection.pred_densepose = densepose_output_i
        k += n_i

def _linear_interpolation_utilities(v_norm, v0_src, size_src, v0_dst, size_dst, size_z):
    """
    Computes utility values for linear interpolation at points v.
    The points are given as normalized offsets in the source interval
    (v0_src, v0_src + size_src), more precisely:
        v = v0_src + v_norm * size_src / 256.0
    The computed utilities include lower points v_lo, upper points v_hi,
    interpolation weights v_w and flags j_valid indicating whether the
    points falls into the destination interval (v0_dst, v0_dst + size_dst).

    Args:
        v_norm (:obj: `torch.Tensor`): tensor of size N containing
            normalized point offsets
        v0_src (:obj: `torch.Tensor`): tensor of size N containing
            left bounds of source intervals for normalized points
        size_src (:obj: `torch.Tensor`): tensor of size N containing
            source interval sizes for normalized points
        v0_dst (:obj: `torch.Tensor`): tensor of size N containing
            left bounds of destination intervals
        size_dst (:obj: `torch.Tensor`): tensor of size N containing
            destination interval sizes
        size_z (int): interval size for data to be interpolated

    Returns:
        v_lo (:obj: `torch.Tensor`): int tensor of size N containing
            indices of lower values used for interpolation, all values are
            integers from [0, size_z - 1]
        v_hi (:obj: `torch.Tensor`): int tensor of size N containing
            indices of upper values used for interpolation, all values are
            integers from [0, size_z - 1]
        v_w (:obj: `torch.Tensor`): float tensor of size N containing
            interpolation weights
        j_valid (:obj: `torch.Tensor`): uint8 tensor of size N containing
            0 for points outside the estimation interval
            (v0_est, v0_est + size_est) and 1 otherwise
    """
    v = v0_src + v_norm * size_src / 256.0
    j_valid = (v - v0_dst >= 0) * (v - v0_dst < size_dst)
    v_grid = (v - v0_dst) * size_z / size_dst
    v_lo = v_grid.floor().long().clamp(min=0, max=size_z - 1)
    v_hi = (v_lo + 1).clamp(max=size_z - 1)
    v_grid = torch.min(v_hi.float(), v_grid)
    v_w = v_grid - v_lo.float()
    return v_lo, v_hi, v_w, j_valid


def _grid_sampling_utilities(
    zh, zw, bbox_xywh_est, bbox_xywh_gt, index_gt, x_norm, y_norm, index_bbox
):
    """
    Prepare tensors used in grid sampling.

    Args:
        z_est (:obj: `torch.Tensor`): tensor of size (N,C,H,W) with estimated
            values of Z to be extracted for the points X, Y and channel
            indices I
        bbox_xywh_est (:obj: `torch.Tensor`): tensor of size (N, 4) containing
            estimated bounding boxes in format XYWH
        bbox_xywh_gt (:obj: `torch.Tensor`): tensor of size (N, 4) containing
            matched ground truth bounding boxes in format XYWH
        index_gt (:obj: `torch.Tensor`): tensor of size K with point labels for
            ground truth points
        x_norm (:obj: `torch.Tensor`): tensor of size K with X normalized
            coordinates of ground truth points. Image X coordinates can be
            obtained as X = Xbbox + x_norm * Wbbox / 255
        y_norm (:obj: `torch.Tensor`): tensor of size K with Y normalized
            coordinates of ground truth points. Image Y coordinates can be
            obtained as Y = Ybbox + y_norm * Hbbox / 255
        index_bbox (:obj: `torch.Tensor`): tensor of size K with bounding box
            indices for each ground truth point. The values are thus in
            [0, N-1]

    Returns:
        j_valid (:obj: `torch.Tensor`): uint8 tensor of size M containing
            0 for points to be discarded and 1 for points to be selected
        y_lo (:obj: `torch.Tensor`): int tensor of indices of upper values
            in z_est for each point
        y_hi (:obj: `torch.Tensor`): int tensor of indices of lower values
            in z_est for each point
        x_lo (:obj: `torch.Tensor`): int tensor of indices of left values
            in z_est for each point
        x_hi (:obj: `torch.Tensor`): int tensor of indices of right values
            in z_est for each point
        w_ylo_xlo (:obj: `torch.Tensor`): float tensor of size M;
            contains upper-left value weight for each point
        w_ylo_xhi (:obj: `torch.Tensor`): float tensor of size M;
            contains upper-right value weight for each point
        w_yhi_xlo (:obj: `torch.Tensor`): float tensor of size M;
            contains lower-left value weight for each point
        w_yhi_xhi (:obj: `torch.Tensor`): float tensor of size M;
            contains lower-right value weight for each point
    """

    x0_gt, y0_gt, w_gt, h_gt = bbox_xywh_gt[index_bbox].unbind(dim=1)
    x0_est, y0_est, w_est, h_est = bbox_xywh_est[index_bbox].unbind(dim=1)
    x_lo, x_hi, x_w, jx_valid = _linear_interpolation_utilities(
        x_norm, x0_gt, w_gt, x0_est, w_est, zw
    )
    y_lo, y_hi, y_w, jy_valid = _linear_interpolation_utilities(
        y_norm, y0_gt, h_gt, y0_est, h_est, zh
    )
    j_valid = jx_valid * jy_valid

    w_ylo_xlo = (1.0 - x_w) * (1.0 - y_w)
    w_ylo_xhi = x_w * (1.0 - y_w)
    w_yhi_xlo = (1.0 - x_w) * y_w
    w_yhi_xhi = x_w * y_w

    return j_valid, y_lo, y_hi, x_lo, x_hi, w_ylo_xlo, w_ylo_xhi, w_yhi_xlo, w_yhi_xhi


def _extract_at_points_packed(
    z_est,
    index_bbox_valid,
    slice_index_uv,
    y_lo,
    y_hi,
    x_lo,
    x_hi,
    w_ylo_xlo,
    w_ylo_xhi,
    w_yhi_xlo,
    w_yhi_xhi,
    block=False,
    block_slice=None,
):
    """
    Extract ground truth values z_gt for valid point indices and estimated
    values z_est using bilinear interpolation over top-left (y_lo, x_lo),
    top-right (y_lo, x_hi), bottom-left (y_hi, x_lo) and bottom-right
    (y_hi, x_hi) values in z_est with corresponding weights:
    w_ylo_xlo, w_ylo_xhi, w_yhi_xlo and w_yhi_xhi.
    Use slice_index_uv to slice dim=1 in z_est
    """
    if block:
        z_est_sampled = (
                z_est[index_bbox_valid, slice_index_uv, block_slice, y_lo, x_lo] * w_ylo_xlo
                + z_est[index_bbox_valid, slice_index_uv, block_slice, y_lo, x_hi] * w_ylo_xhi
                + z_est[index_bbox_valid, slice_index_uv, block_slice, y_hi, x_lo] * w_yhi_xlo
                + z_est[index_bbox_valid, slice_index_uv, block_slice, y_hi, x_hi] * w_yhi_xhi
            )
    else:
        z_est_sampled = (
            z_est[index_bbox_valid, slice_index_uv, y_lo, x_lo] * w_ylo_xlo
            + z_est[index_bbox_valid, slice_index_uv, y_lo, x_hi] * w_ylo_xhi
            + z_est[index_bbox_valid, slice_index_uv, y_hi, x_lo] * w_yhi_xlo
            + z_est[index_bbox_valid, slice_index_uv, y_hi, x_hi] * w_yhi_xhi
        )
    return z_est_sampled


def _resample_data(
    z, bbox_xywh_src, bbox_xywh_dst, wout, hout, mode="nearest", padding_mode="zeros"
):
    """
    Args:
        z (:obj: `torch.Tensor`): tensor of size (N,C,H,W) with data to be
            resampled
        bbox_xywh_src (:obj: `torch.Tensor`): tensor of size (N,4) containing
            source bounding boxes in format XYWH
        bbox_xywh_dst (:obj: `torch.Tensor`): tensor of size (N,4) containing
            destination bounding boxes in format XYWH
    Return:
        zresampled (:obj: `torch.Tensor`): tensor of size (N, C, Hout, Wout)
            with resampled values of z, where D is the discretization size
    """
    n = bbox_xywh_src.size(0)
    assert n == bbox_xywh_dst.size(0), (
        "The number of "
        "source ROIs for resampling ({}) should be equal to the number "
        "of destination ROIs ({})".format(bbox_xywh_src.size(0), bbox_xywh_dst.size(0))
    )
    x0src, y0src, wsrc, hsrc = bbox_xywh_src.unbind(dim=1)
    x0dst, y0dst, wdst, hdst = bbox_xywh_dst.unbind(dim=1)
    x0dst_norm = 2 * (x0dst - x0src) / wsrc - 1
    y0dst_norm = 2 * (y0dst - y0src) / hsrc - 1
    x1dst_norm = 2 * (x0dst + wdst - x0src) / wsrc - 1
    y1dst_norm = 2 * (y0dst + hdst - y0src) / hsrc - 1
    grid_w = torch.arange(wout, device=z.device, dtype=torch.float) / wout
    grid_h = torch.arange(hout, device=z.device, dtype=torch.float) / hout
    grid_w_expanded = grid_w[None, None, :].expand(n, hout, wout)
    grid_h_expanded = grid_h[None, :, None].expand(n, hout, wout)
    dx_expanded = (x1dst_norm - x0dst_norm)[:, None, None].expand(n, hout, wout)
    dy_expanded = (y1dst_norm - y0dst_norm)[:, None, None].expand(n, hout, wout)
    x0_expanded = x0dst_norm[:, None, None].expand(n, hout, wout)
    y0_expanded = y0dst_norm[:, None, None].expand(n, hout, wout)
    grid_x = grid_w_expanded * dx_expanded + x0_expanded
    grid_y = grid_h_expanded * dy_expanded + y0_expanded
    grid = torch.stack((grid_x, grid_y), dim=3)
    # resample Z from (N, C, H, W) into (N, C, Hout, Wout)
    zresampled = F.grid_sample(z, grid, mode=mode, padding_mode=padding_mode, align_corners=True)
    return zresampled


def _extract_single_tensors_from_matches_one_image(
    proposals_targets, bbox_with_dp_offset, bbox_global_offset
):
    i_gt_all = []
    x_norm_all = []
    y_norm_all = []
    u_gt_all = []
    v_gt_all = []
    s_gt_all = []
    bbox_xywh_gt_all = []
    bbox_xywh_est_all = []
    # Ibbox_all == k should be true for all data that corresponds
    # to bbox_xywh_gt[k] and bbox_xywh_est[k]
    # index k here is global wrt images
    i_bbox_all = []
    # at offset k (k is global) contains index of bounding box data
    # within densepose output tensor
    i_with_dp = []

    boxes_xywh_est = proposals_targets.proposal_boxes.clone()
    boxes_xywh_gt = proposals_targets.gt_boxes.clone()
    n_i = len(boxes_xywh_est)
    assert n_i == len(boxes_xywh_gt)

    if n_i:
        boxes_xywh_est.tensor[:, 2] -= boxes_xywh_est.tensor[:, 0]
        boxes_xywh_est.tensor[:, 3] -= boxes_xywh_est.tensor[:, 1]
        boxes_xywh_gt.tensor[:, 2] -= boxes_xywh_gt.tensor[:, 0]
        boxes_xywh_gt.tensor[:, 3] -= boxes_xywh_gt.tensor[:, 1]
        if hasattr(proposals_targets, "gt_densepose"):
            densepose_gt = proposals_targets.gt_densepose
            for k, box_xywh_est, box_xywh_gt, dp_gt in zip(
                range(n_i), boxes_xywh_est.tensor, boxes_xywh_gt.tensor, densepose_gt
            ):
                if (dp_gt is not None) and (len(dp_gt.x) > 0):
                    i_gt_all.append(dp_gt.i)
                    x_norm_all.append(dp_gt.x)
                    y_norm_all.append(dp_gt.y)
                    u_gt_all.append(dp_gt.u)
                    v_gt_all.append(dp_gt.v)
                    s_gt_all.append(dp_gt.segm.unsqueeze(0))
                    bbox_xywh_gt_all.append(box_xywh_gt.view(-1, 4))
                    bbox_xywh_est_all.append(box_xywh_est.view(-1, 4))
                    i_bbox_k = torch.full_like(dp_gt.i, bbox_with_dp_offset + len(i_with_dp))
                    i_bbox_all.append(i_bbox_k)
                    i_with_dp.append(bbox_global_offset + k)
    return (
        i_gt_all,
        x_norm_all,
        y_norm_all,
        u_gt_all,
        v_gt_all,
        s_gt_all,
        bbox_xywh_gt_all,
        bbox_xywh_est_all,
        i_bbox_all,
        i_with_dp,
    )


def _extract_single_tensors_from_matches(proposals_with_targets):
    i_img = []
    i_gt_all = []
    x_norm_all = []
    y_norm_all = []
    u_gt_all = []
    v_gt_all = []
    s_gt_all = []
    bbox_xywh_gt_all = []
    bbox_xywh_est_all = []
    i_bbox_all = []
    i_with_dp_all = []
    n = 0
    for i, proposals_targets_per_image in enumerate(proposals_with_targets):
        n_i = proposals_targets_per_image.proposal_boxes.tensor.size(0)
        if not n_i:
            continue
        (
            i_gt_img,
            x_norm_img,
            y_norm_img,
            u_gt_img,
            v_gt_img,
            s_gt_img,
            bbox_xywh_gt_img,
            bbox_xywh_est_img,
            i_bbox_img,
            i_with_dp_img,
        ) = _extract_single_tensors_from_matches_one_image(  # noqa
            proposals_targets_per_image, len(i_with_dp_all), n
        )
        i_gt_all.extend(i_gt_img)
        x_norm_all.extend(x_norm_img)
        y_norm_all.extend(y_norm_img)
        u_gt_all.extend(u_gt_img)
        v_gt_all.extend(v_gt_img)
        s_gt_all.extend(s_gt_img)
        bbox_xywh_gt_all.extend(bbox_xywh_gt_img)
        bbox_xywh_est_all.extend(bbox_xywh_est_img)
        i_bbox_all.extend(i_bbox_img)
        i_with_dp_all.extend(i_with_dp_img)
        i_img.extend([i] * len(i_with_dp_img))
        n += n_i
    # concatenate all data into a single tensor
    if (n > 0) and (len(i_with_dp_all) > 0):
        i_gt = torch.cat(i_gt_all, 0).long()
        x_norm = torch.cat(x_norm_all, 0)
        y_norm = torch.cat(y_norm_all, 0)
        u_gt = torch.cat(u_gt_all, 0)
        v_gt = torch.cat(v_gt_all, 0)
        s_gt = torch.cat(s_gt_all, 0)
        bbox_xywh_gt = torch.cat(bbox_xywh_gt_all, 0)
        bbox_xywh_est = torch.cat(bbox_xywh_est_all, 0)
        i_bbox = torch.cat(i_bbox_all, 0).long()
    else:
        i_gt = None
        x_norm = None
        y_norm = None
        u_gt = None
        v_gt = None
        s_gt = None
        bbox_xywh_gt = None
        bbox_xywh_est = None
        i_bbox = None
    return (
        i_img,
        i_with_dp_all,
        bbox_xywh_est,
        bbox_xywh_gt,
        i_gt,
        x_norm,
        y_norm,
        u_gt,
        v_gt,
        s_gt,
        i_bbox,
    )


@dataclass
class DataForMaskLoss:
    """
    Contains mask GT and estimated data for proposals from multiple images:
    """

    # tensor of size (K, H, W) containing GT labels
    masks_gt: Optional[torch.Tensor] = None
    # tensor of size (K, C, H, W) containing estimated scores
    masks_est: Optional[torch.Tensor] = None


def _extract_data_for_mask_loss_from_matches(
    proposals_targets: Iterable[Instances], estimated_segm: torch.Tensor
) -> DataForMaskLoss:
    """
    Extract data for mask loss from instances that contain matched GT and
    estimated bounding boxes.
    Args:
        proposals_targets: Iterable[Instances]
            matched GT and estimated results, each item in the iterable
            corresponds to data in 1 image
        estimated_segm: torch.Tensor if size
            size to which GT masks are resized
    Return:
        masks_est: tensor(K, C, H, W) of float - class scores
        masks_gt: tensor(K, H, W) of int64 - labels
    """
    data = DataForMaskLoss()
    masks_gt = []
    offset = 0
    assert estimated_segm.shape[2] == estimated_segm.shape[3], (
        f"Expected estimated segmentation to have a square shape, "
        f"but the actual shape is {estimated_segm.shape[2:]}"
    )
    mask_size = estimated_segm.shape[2]
    num_proposals = sum(inst.proposal_boxes.tensor.size(0) for inst in proposals_targets)
    num_estimated = estimated_segm.shape[0]
    assert (
        num_proposals == num_estimated
    ), "The number of proposals {} must be equal to the number of estimates {}".format(
        num_proposals, num_estimated
    )

    for proposals_targets_per_image in proposals_targets:
        n_i = proposals_targets_per_image.proposal_boxes.tensor.size(0)
        if not n_i:
            continue
        gt_masks_per_image = proposals_targets_per_image.gt_masks.crop_and_resize(
            proposals_targets_per_image.proposal_boxes.tensor, mask_size
        ).to(device=estimated_segm.device)
        masks_gt.append(gt_masks_per_image)
        offset += n_i
    if masks_gt:
        data.masks_est = estimated_segm
        data.masks_gt = torch.cat(masks_gt, dim=0)
    return data


class IIDIsotropicGaussianUVLoss(nn.Module):
    """
    Loss for the case of iid residuals with isotropic covariance:
    $Sigma_i = sigma_i^2 I$
    The loss (negative log likelihood) is then:
    $1/2 sum_{i=1}^n (log(2 pi) + 2 log sigma_i^2 + ||delta_i||^2 / sigma_i^2)$,
    where $delta_i=(u - u', v - v')$ is a 2D vector containing UV coordinates
    difference between estimated and ground truth UV values
    For details, see:
    N. Neverova, D. Novotny, A. Vedaldi "Correlated Uncertainty for Learning
    Dense Correspondences from Noisy Labels", p. 918--926, in Proc. NIPS 2019
    """

    def __init__(self, sigma_lower_bound: float):
        super(IIDIsotropicGaussianUVLoss, self).__init__()
        self.sigma_lower_bound = sigma_lower_bound
        self.log2pi = math.log(2 * math.pi)

    def forward(
        self,
        u: torch.Tensor,
        v: torch.Tensor,
        sigma_u: torch.Tensor,
        target_u: torch.Tensor,
        target_v: torch.Tensor,
    ):
        # compute $\sigma_i^2$
        # use sigma_lower_bound to avoid degenerate solution for variance
        # (sigma -> 0)
        sigma2 = F.softplus(sigma_u) + self.sigma_lower_bound
        # compute \|delta_i\|^2
        delta_t_delta = (u - target_u) ** 2 + (v - target_v) ** 2
        # the total loss from the formula above:
        loss = 0.5 * (self.log2pi + 2 * torch.log(sigma2) + delta_t_delta / sigma2)
        return loss.sum()


class IndepAnisotropicGaussianUVLoss(nn.Module):
    """
    Loss for the case of independent residuals with anisotropic covariances:
    $Sigma_i = sigma_i^2 I + r_i r_i^T$
    The loss (negative log likelihood) is then:
    $1/2 sum_{i=1}^n (log(2 pi)
      + log sigma_i^2 (sigma_i^2 + ||r_i||^2)
      + ||delta_i||^2 / sigma_i^2
      - <delta_i, r_i>^2 / (sigma_i^2 * (sigma_i^2 + ||r_i||^2)))$,
    where $delta_i=(u - u', v - v')$ is a 2D vector containing UV coordinates
    difference between estimated and ground truth UV values
    For details, see:
    N. Neverova, D. Novotny, A. Vedaldi "Correlated Uncertainty for Learning
    Dense Correspondences from Noisy Labels", p. 918--926, in Proc. NIPS 2019
    """

    def __init__(self, sigma_lower_bound: float):
        super(IndepAnisotropicGaussianUVLoss, self).__init__()
        self.sigma_lower_bound = sigma_lower_bound
        self.log2pi = math.log(2 * math.pi)

    def forward(
        self,
        u: torch.Tensor,
        v: torch.Tensor,
        sigma_u: torch.Tensor,
        kappa_u_est: torch.Tensor,
        kappa_v_est: torch.Tensor,
        target_u: torch.Tensor,
        target_v: torch.Tensor,
    ):
        # compute $\sigma_i^2$
        sigma2 = F.softplus(sigma_u) + self.sigma_lower_bound
        # compute \|r_i\|^2
        r_sqnorm2 = kappa_u_est ** 2 + kappa_v_est ** 2
        delta_u = u - target_u
        delta_v = v - target_v
        # compute \|delta_i\|^2
        delta_sqnorm = delta_u ** 2 + delta_v ** 2
        delta_u_r_u = delta_u * kappa_u_est
        delta_v_r_v = delta_v * kappa_v_est
        # compute the scalar product <delta_i, r_i>
        delta_r = delta_u_r_u + delta_v_r_v
        # compute squared scalar product <delta_i, r_i>^2
        delta_r_sqnorm = delta_r ** 2
        denom2 = sigma2 * (sigma2 + r_sqnorm2)
        loss = 0.5 * (
            self.log2pi + torch.log(denom2) + delta_sqnorm / sigma2 - delta_r_sqnorm / denom2
        )
        return loss.sum()


class DensePoseLosses(object):
    def __init__(self, cfg):
        # fmt: off
        self.heatmap_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.HEATMAP_SIZE
        self.w_points     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POINT_REGRESSION_WEIGHTS
        self.w_part       = cfg.MODEL.ROI_DENSEPOSE_HEAD.PART_WEIGHTS
        self.w_segm       = cfg.MODEL.ROI_DENSEPOSE_HEAD.INDEX_WEIGHTS
        self.n_segm_chan  = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        # fmt: on
        self.segm_trained_by_masks = cfg.MODEL.ROI_DENSEPOSE_HEAD.COARSE_SEGM_TRAINED_BY_MASKS
        self.confidence_model_cfg = DensePoseConfidenceModelConfig.from_cfg(cfg)
        if self.confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.IID_ISO:
            self.uv_loss_with_confidences = IIDIsotropicGaussianUVLoss(
                self.confidence_model_cfg.uv_confidence.epsilon
            )
        elif self.confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.INDEP_ANISO:
            self.uv_loss_with_confidences = IndepAnisotropicGaussianUVLoss(
                self.confidence_model_cfg.uv_confidence.epsilon
            )

    def __call__(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        if not self.segm_trained_by_masks:
            losses = {}
            densepose_losses = self.produce_densepose_losses(
                proposals_with_gt, densepose_outputs[:4], densepose_confidences
            )
            losses.update(densepose_losses)
            return losses
        else:
            losses = {}
            losses_densepose = self.produce_densepose_losses(
                proposals_with_gt, densepose_outputs, densepose_confidences
            )
            losses.update(losses_densepose)
            losses_mask = self.produce_mask_losses(
                proposals_with_gt, densepose_outputs, densepose_confidences
            )
            losses.update(losses_mask)
            return losses

    def produce_fake_mask_losses(self, densepose_outputs):
        losses = {}
        segm_scores, _, _, _ = densepose_outputs
        losses["loss_densepose_S"] = segm_scores.sum() * 0
        return losses

    def produce_mask_losses(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        if not len(proposals_with_gt):
            return self.produce_fake_mask_losses(densepose_outputs)
        losses = {}
        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7
        segm_scores, _, _, _ = densepose_outputs
        with torch.no_grad():
            mask_loss_data = _extract_data_for_mask_loss_from_matches(
                proposals_with_gt, segm_scores
            )
        if (mask_loss_data.masks_gt is None) or (mask_loss_data.masks_est is None):
            return self.produce_fake_mask_losses(densepose_outputs)
        losses["loss_densepose_S"] = (
            F.cross_entropy(mask_loss_data.masks_est, mask_loss_data.masks_gt.long()) * self.w_segm
        )
        return losses

    def produce_fake_densepose_losses(self, densepose_outputs, densepose_confidences):
        # we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) losses in the form Tensor.sum() * 0
        s, index_uv, u, v = densepose_outputs
        conf_type = self.confidence_model_cfg.uv_confidence.type
        (
            sigma_1,
            sigma_2,
            kappa_u,
            kappa_v,
            fine_segm_confidence,
            coarse_segm_confidence,
        ) = densepose_confidences
        losses = {}
        losses["loss_densepose_I"] = index_uv.sum() * 0
        if not self.segm_trained_by_masks:
            losses["loss_densepose_S"] = s.sum() * 0
        if self.confidence_model_cfg.uv_confidence.enabled:
            losses["loss_densepose_UV"] = (u.sum() + v.sum()) * 0
            if conf_type == DensePoseUVConfidenceType.IID_ISO:
                losses["loss_densepose_UV"] += sigma_2.sum() * 0
            elif conf_type == DensePoseUVConfidenceType.INDEP_ANISO:
                losses["loss_densepose_UV"] += (sigma_2.sum() + kappa_u.sum() + kappa_v.sum()) * 0
        else:
            losses["loss_densepose_U"] = u.sum() * 0
            losses["loss_densepose_V"] = v.sum() * 0
        return losses

    def produce_densepose_losses(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        losses = {}
        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7
        s, index_uv, u, v = densepose_outputs
        if not len(proposals_with_gt):
            return self.produce_fake_densepose_losses(densepose_outputs, densepose_confidences)
        (
            sigma_1,
            sigma_2,
            kappa_u,
            kappa_v,
            fine_segm_confidence,
            coarse_segm_confidence,
        ) = densepose_confidences
        conf_type = self.confidence_model_cfg.uv_confidence.type
        assert u.size(2) == v.size(2)
        assert u.size(3) == v.size(3)
        assert u.size(2) == index_uv.size(2)
        assert u.size(3) == index_uv.size(3)

        with torch.no_grad():
            (
                index_uv_img,
                i_with_dp,
                bbox_xywh_est,
                bbox_xywh_gt,
                index_gt_all,
                x_norm,
                y_norm,
                u_gt_all,
                v_gt_all,
                s_gt,
                index_bbox,
            ) = _extract_single_tensors_from_matches(  # noqa
                proposals_with_gt
            )
        n_batch = len(i_with_dp)

        # NOTE: we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) loss in the form Tensor.sum() * 0
        if not n_batch:
            return self.produce_fake_densepose_losses(densepose_outputs, densepose_confidences)

        zh = u.size(2)
        zw = u.size(3)

        (
            j_valid,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        ) = _grid_sampling_utilities(  # noqa
            zh, zw, bbox_xywh_est, bbox_xywh_gt, index_gt_all, x_norm, y_norm, index_bbox
        )

        j_valid_fg = j_valid * (index_gt_all > 0)

        u_gt = u_gt_all[j_valid_fg]
        u_est_all = _extract_at_points_packed(
            u[i_with_dp],
            index_bbox,
            index_gt_all,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        )
        u_est = u_est_all[j_valid_fg]

        v_gt = v_gt_all[j_valid_fg]
        v_est_all = _extract_at_points_packed(
            v[i_with_dp],
            index_bbox,
            index_gt_all,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        )
        v_est = v_est_all[j_valid_fg]

        index_uv_gt = index_gt_all[j_valid]
        index_uv_est_all = _extract_at_points_packed(
            index_uv[i_with_dp],
            index_bbox,
            slice(None),
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo[:, None],
            w_ylo_xhi[:, None],
            w_yhi_xlo[:, None],
            w_yhi_xhi[:, None],
        )
        index_uv_est = index_uv_est_all[j_valid, :]

        if self.confidence_model_cfg.uv_confidence.enabled:
            sigma_2_est_all = _extract_at_points_packed(
                sigma_2[i_with_dp],
                index_bbox,
                index_gt_all,
                y_lo,
                y_hi,
                x_lo,
                x_hi,
                w_ylo_xlo,
                w_ylo_xhi,
                w_yhi_xlo,
                w_yhi_xhi,
            )
            sigma_2_est = sigma_2_est_all[j_valid_fg]
            if conf_type in [DensePoseUVConfidenceType.INDEP_ANISO]:
                kappa_u_est_all = _extract_at_points_packed(
                    kappa_u[i_with_dp],
                    index_bbox,
                    index_gt_all,
                    y_lo,
                    y_hi,
                    x_lo,
                    x_hi,
                    w_ylo_xlo,
                    w_ylo_xhi,
                    w_yhi_xlo,
                    w_yhi_xhi,
                )
                kappa_u_est = kappa_u_est_all[j_valid_fg]
                kappa_v_est_all = _extract_at_points_packed(
                    kappa_v[i_with_dp],
                    index_bbox,
                    index_gt_all,
                    y_lo,
                    y_hi,
                    x_lo,
                    x_hi,
                    w_ylo_xlo,
                    w_ylo_xhi,
                    w_yhi_xlo,
                    w_yhi_xhi,
                )
                kappa_v_est = kappa_v_est_all[j_valid_fg]

        # Resample everything to the estimated data size, no need to resample
        # S_est then:
        if not self.segm_trained_by_masks:
            s_est = s[i_with_dp]
            with torch.no_grad():
                s_gt = _resample_data(
                    s_gt.unsqueeze(1),
                    bbox_xywh_gt,
                    bbox_xywh_est,
                    self.heatmap_size,
                    self.heatmap_size,
                    mode="nearest",
                    padding_mode="zeros",
                ).squeeze(1)

        # add point-based losses:
        if self.confidence_model_cfg.uv_confidence.enabled:
            if conf_type == DensePoseUVConfidenceType.IID_ISO:
                uv_loss = (
                    self.uv_loss_with_confidences(u_est, v_est, sigma_2_est, u_gt, v_gt)
                    * self.w_points
                )
                losses["loss_densepose_UV"] = uv_loss
            elif conf_type == DensePoseUVConfidenceType.INDEP_ANISO:
                uv_loss = (
                    self.uv_loss_with_confidences(
                        u_est, v_est, sigma_2_est, kappa_u_est, kappa_v_est, u_gt, v_gt
                    )
                    * self.w_points
                )
                losses["loss_densepose_UV"] = uv_loss
            else:
                raise ValueError(f"Unknown confidence model type: {conf_type}")
        else:
            u_loss = F.smooth_l1_loss(u_est, u_gt, reduction="sum") * self.w_points
            losses["loss_densepose_U"] = u_loss
            v_loss = F.smooth_l1_loss(v_est, v_gt, reduction="sum") * self.w_points
            losses["loss_densepose_V"] = v_loss
        
        # AEQL
        J = index_uv_gt.shape[0]
        M = torch.max(index_uv_est, dim=1, keepdim=True)[0]
        E = self.exclude_func(index_uv_gt, J)
        T = self.threshold_func(index_uv_gt, J)
        y_t = F.one_hot(index_uv_gt, index_uv_est.shape[1])
        prob = torch.softmax(index_uv_est, axis=1).detach()
        top_values, top_index = prob.topk(18, dim=1, largest=False, sorted=True)
        mi = index_uv_est.gather(1, top_index[torch.arange(J),-1].unsqueeze(-1))
        correlation = torch.exp(-(index_uv_est-mi)/(M-mi)).detach()
        correlation = correlation.scatter(1, top_index, 1.)
        eql_w = 1. - E * T * (1. - y_t)*correlation
        x = (index_uv_est-M) - torch.log(torch.sum(eql_w*torch.exp(index_uv_est-M), dim=1)).unsqueeze(1).repeat(1, index_uv_est.shape[1])
        smooth_loss = -x.mean(dim=-1)
        index_uv_loss = torch.sum(F.nll_loss(x, index_uv_gt.long())*0.9 + smooth_loss*0.1) * self.w_part
        
        # index_uv_loss = F.cross_entropy(index_uv_est, index_uv_gt.long()) * self.w_part
        losses["loss_densepose_I"] = index_uv_loss

        if not self.segm_trained_by_masks:
            if self.n_segm_chan == 2:
                s_gt = s_gt > 0
            s_loss = F.cross_entropy(s_est, s_gt.long()) * self.w_segm
            losses["loss_densepose_S"] = s_loss
        return losses
    def exclude_func(self, gt_classes, J):
        weight = torch.zeros((J), dtype=torch.float).cuda()
        beta = torch.Tensor(weight.shape).cuda().uniform_(0,1)
        weight[beta < 0.99] = 1.
        weight = weight.view(J, 1).expand(J, 25)
        return weight

    def threshold_func(self, gt_classes, J): 
        weight = torch.zeros(25).cuda()
        freq = [7,8,11,12]
        for f in freq:
            weight[f] = 1
        weight = weight.unsqueeze(0)
        weight = weight.repeat(J, 1)
        return weight

class SABLLosses(object):
    def __init__(self, cfg):
        # fmt: off
        self.heatmap_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.HEATMAP_SIZE
        self.w_points     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POINT_REGRESSION_WEIGHTS
        self.w_part       = cfg.MODEL.ROI_DENSEPOSE_HEAD.PART_WEIGHTS
        self.w_segm       = cfg.MODEL.ROI_DENSEPOSE_HEAD.INDEX_WEIGHTS
        self.n_segm_chan  = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS
        # fmt: on
        self.segm_trained_by_masks = cfg.MODEL.ROI_DENSEPOSE_HEAD.COARSE_SEGM_TRAINED_BY_MASKS
        self.confidence_model_cfg = DensePoseConfidenceModelConfig.from_cfg(cfg)
        if self.confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.IID_ISO:
            self.uv_loss_with_confidences = IIDIsotropicGaussianUVLoss(
                self.confidence_model_cfg.uv_confidence.epsilon
            )
        elif self.confidence_model_cfg.uv_confidence.type == DensePoseUVConfidenceType.INDEP_ANISO:
            self.uv_loss_with_confidences = IndepAnisotropicGaussianUVLoss(
                self.confidence_model_cfg.uv_confidence.epsilon
            )
        
        block_fpath = cfg.MODEL.ROI_DENSEPOSE_HEAD.BLOCK_FPATH
        with open(block_fpath, "rb") as bFile:
            self.block_file = json.loads(json.load(bFile))
        self.w_poise_cls     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POISE_CLS_WEIGHTS
        self.w_poise_reg     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POISE_REGRESSION_WEIGHTS

    def __call__(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        if not self.segm_trained_by_masks:
            losses = {}
            densepose_losses = self.produce_densepose_losses(
                proposals_with_gt, densepose_outputs[:6], densepose_confidences
            )
            losses.update(densepose_losses)
            return losses
        else:
            losses = {}
            losses_densepose = self.produce_densepose_losses(
                proposals_with_gt, densepose_outputs, densepose_confidences
            )
            losses.update(losses_densepose)
            losses_mask = self.produce_mask_losses(
                proposals_with_gt, densepose_outputs, densepose_confidences
            )
            losses.update(losses_mask)
            return losses

    def produce_fake_mask_losses(self, densepose_outputs):
        losses = {}
        segm_scores, _, _, _ = densepose_outputs
        losses["loss_densepose_S"] = segm_scores.sum() * 0
        return losses

    def produce_mask_losses(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        if not len(proposals_with_gt):
            return self.produce_fake_mask_losses(densepose_outputs)
        losses = {}
        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7
        segm_scores, _, _, _ = densepose_outputs
        with torch.no_grad():
            mask_loss_data = _extract_data_for_mask_loss_from_matches(
                proposals_with_gt, segm_scores
            )
        if (mask_loss_data.masks_gt is None) or (mask_loss_data.masks_est is None):
            return self.produce_fake_mask_losses(densepose_outputs)
        losses["loss_densepose_S"] = (
            F.cross_entropy(mask_loss_data.masks_est, mask_loss_data.masks_gt.long()) * self.w_segm
        )
        return losses

    def produce_fake_densepose_losses(self, densepose_outputs, densepose_confidences):
        # we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) losses in the form Tensor.sum() * 0
        s, index_uv, u_cls, u_offset, v_cls, v_offset = densepose_outputs
        losses = {}
        losses["loss_densepose_I"] = index_uv.sum() * 0
        if not self.segm_trained_by_masks:
            losses["loss_densepose_S"] = s.sum() * 0
        losses["loss_densepose_U_cls"] = u_cls.sum() * 0
        losses["loss_densepose_U_offset"] = u_cls.sum() * 0
        losses["loss_densepose_V_cls"] = v_cls.sum() * 0
        losses["loss_densepose_V_offset"] = v_cls.sum() * 0
        return losses

    def produce_densepose_losses(self, proposals_with_gt, densepose_outputs, densepose_confidences):
        losses = {}
        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7
        s, index_uv, u_cls, u_offset, v_cls, v_offset = densepose_outputs
        if not len(proposals_with_gt):
            return self.produce_fake_densepose_losses(densepose_outputs, densepose_confidences)

        assert u_cls.size(2) == v_cls.size(2)
        assert u_cls.size(3) == v_cls.size(3)
        assert u_cls.size(2) == index_uv.size(2)
        assert u_cls.size(3) == index_uv.size(3)

        with torch.no_grad():
            (
                index_uv_img,
                i_with_dp,
                bbox_xywh_est,
                bbox_xywh_gt,
                index_gt_all,
                x_norm,
                y_norm,
                u_gt_all,
                v_gt_all,
                s_gt,
                index_bbox,
            ) = _extract_single_tensors_from_matches(  # noqa
                proposals_with_gt
            )
        n_batch = len(i_with_dp)
        if not n_batch:
            return self.produce_fake_densepose_losses(densepose_outputs, densepose_confidences)

        est_shape = u_cls.shape
        u_cls = u_cls.reshape(-1, 24, BLOBK_NUM, est_shape[2], est_shape[3])
        u_offset = u_offset.reshape(-1, 24, BLOBK_NUM, est_shape[2], est_shape[3])
        v_cls = v_cls.reshape(-1, 24, BLOBK_NUM, est_shape[2], est_shape[3])
        v_offset = v_offset.reshape(-1, 24, BLOBK_NUM, est_shape[2], est_shape[3])

        block_u_width = torch.tensor(np.array(self.block_file["bucket_u_width"]), dtype=torch.float32, device=u_gt_all.device)
        block_v_width = torch.tensor(np.array(self.block_file["bucket_v_width"]), dtype=torch.float32, device=v_gt_all.device)
        block_u_center = torch.tensor(np.array(self.block_file["bucket_u_center"]), dtype=torch.float32, device=u_gt_all.device)
        block_v_center = torch.tensor(np.array(self.block_file["bucket_v_center"]), dtype=torch.float32, device=v_gt_all.device)
        u_gt_cls, u_gt_offsets = uvToBlocks(u_gt_all, block_u_width, block_u_center, index_gt_all-1, block_u_width.shape[1])
        v_gt_cls, v_gt_offsets = uvToBlocks(v_gt_all, block_v_width, block_v_center, index_gt_all-1, block_v_width.shape[1])
        del block_u_width, block_v_width, block_u_center, block_v_center
        # NOTE: we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) loss in the form Tensor.sum() * 0

        zh = u_cls.size(3)
        zw = u_cls.size(4)

        (
            j_valid,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        ) = _grid_sampling_utilities(  # noqa
            zh, zw, bbox_xywh_est, bbox_xywh_gt, index_gt_all, x_norm, y_norm, index_bbox
        )

        j_valid_fg = j_valid * (index_gt_all > 0)

        # print(est_shape)
        # print(index_bbox.shape)
        # print(index_gt_all.shape)
        # print(u_gt_cls.shape)
        u_est_cls = _extract_at_points_packed(
            u_cls[i_with_dp],
            index_bbox,
            index_gt_all-1,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo[:, None],
            w_ylo_xhi[:, None],
            w_yhi_xlo[:, None],
            w_yhi_xhi[:, None],
            block = True,
            block_slice = slice(None),
        )[j_valid_fg,:]
        u_est_offsets = _extract_at_points_packed(
            u_offset[i_with_dp],
            index_bbox,
            index_gt_all-1,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
            block = True,
            block_slice = u_gt_cls,
        )[j_valid_fg] # J
        v_est_cls = _extract_at_points_packed(
            v_cls[i_with_dp],
            index_bbox,
            index_gt_all-1,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo[:, None],
            w_ylo_xhi[:, None],
            w_yhi_xlo[:, None],
            w_yhi_xhi[:, None],
            block = True,
            block_slice = slice(None),
        )[j_valid_fg,:]
        v_est_offsets = _extract_at_points_packed(
            v_offset[i_with_dp],
            index_bbox,
            index_gt_all-1,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
            block = True,
            block_slice = v_gt_cls,
        )[j_valid_fg] # J
        del u_cls, u_offset, v_cls, v_offset
        
        u_gt_cls = u_gt_cls[j_valid_fg]
        v_gt_cls = v_gt_cls[j_valid_fg]
        u_gt_offsets = u_gt_offsets[j_valid_fg] # J*2
        v_gt_offsets = v_gt_offsets[j_valid_fg]

        index_uv_gt = index_gt_all[j_valid]
        # print(index_uv[i_with_dp].shape)
        # print(index_bbox.shape)
        index_uv_est_all = _extract_at_points_packed(
            index_uv[i_with_dp],
            index_bbox,
            slice(None),
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo[:, None],
            w_ylo_xhi[:, None],
            w_yhi_xlo[:, None],
            w_yhi_xhi[:, None],
        )
        index_uv_est = index_uv_est_all[j_valid, :]

        # Resample everything to the estimated data size, no need to resample
        # S_est then:
        if not self.segm_trained_by_masks:
            s_est = s[i_with_dp]
            with torch.no_grad():
                s_gt = _resample_data(
                    s_gt.unsqueeze(1),
                    bbox_xywh_gt,
                    bbox_xywh_est,
                    self.heatmap_size,
                    self.heatmap_size,
                    mode="nearest",
                    padding_mode="zeros",
                ).squeeze(1)

        # add point-based losses:
        losses["loss_densepose_U_cls"] = torch.sum(self.labelSmoothing(u_est_cls, u_gt_cls.long()))*self.w_poise_cls
        losses["loss_densepose_U_offset"] = F.smooth_l1_loss(u_est_offsets, u_gt_offsets, reduction="sum")*self.w_poise_reg
        losses["loss_densepose_V_cls"] = torch.sum(self.labelSmoothing(v_est_cls, v_gt_cls.long()))*self.w_poise_cls
        losses["loss_densepose_V_offset"] = F.smooth_l1_loss(v_est_offsets, v_gt_offsets, reduction="sum")*self.w_poise_reg
        
        # AEQL
        J = index_uv_gt.shape[0]
        M = torch.max(index_uv_est, dim=1, keepdim=True)[0]
        E = self.exclude_func(index_uv_gt, J)
        T = self.threshold_func(index_uv_gt, J)
        y_t = F.one_hot(index_uv_gt, index_uv_est.shape[1])
        prob = torch.softmax(index_uv_est, axis=1).detach()
        top_values, top_index = prob.topk(18, dim=1, largest=False, sorted=True)
        mi = index_uv_est.gather(1, top_index[torch.arange(J),-1].unsqueeze(-1))
        correlation = torch.exp(-(index_uv_est-mi)/(M-mi)).detach()
        correlation = correlation.scatter(1, top_index, 1.)
        eql_w = 1. - E * T * (1. - y_t)*correlation
        x = (index_uv_est-M) - torch.log(torch.sum(eql_w*torch.exp(index_uv_est-M), dim=1)).unsqueeze(1).repeat(1, index_uv_est.shape[1])
        smooth_loss = -x.mean(dim=-1)
        index_uv_loss = torch.sum(F.nll_loss(x, index_uv_gt.long())*0.9 + smooth_loss*0.1) * self.w_part
        
        # index_uv_loss = F.cross_entropy(index_uv_est, index_uv_gt.long()) * self.w_part
        losses["loss_densepose_I"] = index_uv_loss

        if not self.segm_trained_by_masks:
            if self.n_segm_chan == 2:
                s_gt = s_gt > 0
            s_loss = F.cross_entropy(s_est, s_gt.long()) * self.w_segm
            losses["loss_densepose_S"] = s_loss
        return losses
    def labelSmoothing(self, x, target, bce=False):
        logprobs = F.log_softmax(x, dim=-1)
        nll_loss = F.nll_loss(logprobs, target)
        smooth_loss = -logprobs.mean(dim=-1)
        loss = nll_loss*0.9 + smooth_loss*0.1
        return loss
    def exclude_func(self, gt_classes, J):
        weight = torch.zeros((J), dtype=torch.float).cuda()
        beta = torch.Tensor(weight.shape).cuda().uniform_(0,1)
        weight[beta < 0.99] = 1.
        weight = weight.view(J, 1).expand(J, 25)
        return weight

    def threshold_func(self, gt_classes, J): 
        weight = torch.zeros(25).cuda()
        freq = [7,8,11,12]
        for f in freq:
            weight[f] = 1
        weight = weight.unsqueeze(0)
        weight = weight.repeat(J, 1)
        return weight

def build_densepose_losses(cfg):
    losses = DensePoseLosses(cfg)
    return losses

def build_sabl_losses(cfg):
    losses = SABLLosses(cfg)
    return losses

def _extract_single_tensors_from_matches_one_image_v2(
    proposals_targets, bbox_with_dp_offset, bbox_global_offset
):
    i_gt_all = []
    x_norm_all = []
    y_norm_all = []
    u_gt_all = []
    v_gt_all = []
    s_gt_all = []
    m_gt_all = []
    bbox_xywh_gt_all = []
    bbox_xywh_est_all = []
    # Ibbox_all == k should be true for all data that corresponds
    # to bbox_xywh_gt[k] and bbox_xywh_est[k]
    # index k here is global wrt images
    i_bbox_all = []
    # at offset k (k is global) contains index of bounding box data
    # within densepose output tensor
    i_with_dp = []

    boxes_xywh_est = proposals_targets.proposal_boxes.clone()
    boxes_xywh_gt = proposals_targets.gt_boxes.clone()
    n_i = len(boxes_xywh_est)
    assert n_i == len(boxes_xywh_gt)

    if n_i:
        boxes_xywh_est.tensor[:, 2] -= boxes_xywh_est.tensor[:, 0]
        boxes_xywh_est.tensor[:, 3] -= boxes_xywh_est.tensor[:, 1]
        boxes_xywh_gt.tensor[:, 2] -= boxes_xywh_gt.tensor[:, 0]
        boxes_xywh_gt.tensor[:, 3] -= boxes_xywh_gt.tensor[:, 1]
        if hasattr(proposals_targets, "gt_densepose"):
            densepose_gt = proposals_targets.gt_densepose
            for k, box_xywh_est, box_xywh_gt, dp_gt in zip(
                range(n_i), boxes_xywh_est.tensor, boxes_xywh_gt.tensor, densepose_gt
            ):
                if (dp_gt is not None) and (len(dp_gt.x) > 0):
                    i_gt_all.append(dp_gt.i)
                    x_norm_all.append(dp_gt.x)
                    y_norm_all.append(dp_gt.y)
                    u_gt_all.append(dp_gt.u)
                    v_gt_all.append(dp_gt.v)
                    s_gt_all.append(dp_gt.segm.unsqueeze(0))
                    #
                    m_gt = dp_gt.segm.clone()
                    m_gt[m_gt>0] = 1
                    m_gt_all.append(m_gt.unsqueeze(0))
                    #
                    bbox_xywh_gt_all.append(box_xywh_gt.view(-1, 4))
                    bbox_xywh_est_all.append(box_xywh_est.view(-1, 4))
                    i_bbox_k = torch.full_like(dp_gt.i, bbox_with_dp_offset + len(i_with_dp))
                    i_bbox_all.append(i_bbox_k)
                    i_with_dp.append(bbox_global_offset + k)
    return (
        i_gt_all,
        x_norm_all,
        y_norm_all,
        u_gt_all,
        v_gt_all,
        s_gt_all,
        m_gt_all,
        bbox_xywh_gt_all,
        bbox_xywh_est_all,
        i_bbox_all,
        i_with_dp,
    )


def _extract_single_tensors_from_matches_v2(proposals_with_targets):
    i_img = []
    i_gt_all = []
    x_norm_all = []
    y_norm_all = []
    u_gt_all = []
    v_gt_all = []
    s_gt_all = []
    m_gt_all = []
    bbox_xywh_gt_all = []
    bbox_xywh_est_all = []
    i_bbox_all = []
    i_with_dp_all = []
    n = 0
    for i, proposals_targets_per_image in enumerate(proposals_with_targets):
        n_i = proposals_targets_per_image.proposal_boxes.tensor.size(0)
        if not n_i:
            continue
        i_gt_img, x_norm_img, y_norm_img, u_gt_img, v_gt_img, s_gt_img, m_gt_img, bbox_xywh_gt_img, bbox_xywh_est_img, i_bbox_img, i_with_dp_img = _extract_single_tensors_from_matches_one_image_v2(  # noqa
            proposals_targets_per_image, len(i_with_dp_all), n
        )
        i_gt_all.extend(i_gt_img)
        x_norm_all.extend(x_norm_img)
        y_norm_all.extend(y_norm_img)
        u_gt_all.extend(u_gt_img)
        v_gt_all.extend(v_gt_img)
        s_gt_all.extend(s_gt_img)
        m_gt_all.extend(m_gt_img)
        bbox_xywh_gt_all.extend(bbox_xywh_gt_img)
        bbox_xywh_est_all.extend(bbox_xywh_est_img)
        i_bbox_all.extend(i_bbox_img)
        i_with_dp_all.extend(i_with_dp_img)
        i_img.extend([i] * len(i_with_dp_img))
        n += n_i
    # concatenate all data into a single tensor
    if (n > 0) and (len(i_with_dp_all) > 0):
        i_gt = torch.cat(i_gt_all, 0).long()
        x_norm = torch.cat(x_norm_all, 0)
        y_norm = torch.cat(y_norm_all, 0)
        u_gt = torch.cat(u_gt_all, 0)
        v_gt = torch.cat(v_gt_all, 0)
        s_gt = torch.cat(s_gt_all, 0)
        m_gt = torch.cat(m_gt_all, 0)
        bbox_xywh_gt = torch.cat(bbox_xywh_gt_all, 0)
        bbox_xywh_est = torch.cat(bbox_xywh_est_all, 0)
        i_bbox = torch.cat(i_bbox_all, 0).long()
    else:
        i_gt = None
        x_norm = None
        y_norm = None
        u_gt = None
        v_gt = None
        s_gt = None
        m_gt = None
        bbox_xywh_gt = None
        bbox_xywh_est = None
        i_bbox = None
    return (
        i_img,
        i_with_dp_all,
        bbox_xywh_est,
        bbox_xywh_gt,
        i_gt,
        x_norm,
        y_norm,
        u_gt,
        v_gt,
        s_gt,
        m_gt,
        i_bbox,
    )

class KTNLosses(object):
    def __init__(self, cfg):
        # fmt: off
        self.heatmap_size = cfg.MODEL.ROI_DENSEPOSE_HEAD.HEATMAP_SIZE # 112
        self.w_points     = cfg.MODEL.ROI_DENSEPOSE_HEAD.POINT_REGRESSION_WEIGHTS # 0.1 / 0.01
        self.w_part       = cfg.MODEL.ROI_DENSEPOSE_HEAD.PART_WEIGHTS # 0.3 / 0.1
        self.w_segm       = cfg.MODEL.ROI_DENSEPOSE_HEAD.INDEX_WEIGHTS # 2.0 / None (14)
        self.w_mask       = cfg.MODEL.ROI_DENSEPOSE_HEAD.BODY_MASK_WEIGHTS # 2.0 / 5.0 (2)
        self.n_segm_chan  = cfg.MODEL.ROI_DENSEPOSE_HEAD.NUM_COARSE_SEGM_CHANNELS

    def __call__(self, proposals_with_gt, densepose_outputs):
        losses = {}

        # densepose outputs are computed for all images and all bounding boxes;
        # i.e. if a batch has 4 images with (3, 1, 2, 1) proposals respectively,
        # the outputs will have size(0) == 3+1+2+1 == 7
        # s, index_uv, u, v, m = densepose_outputs
        m, index_uv, u, v, s = densepose_outputs
        if s.size(1) == 2:
            m, s = s, m
        assert s.size(1) == 15
        assert m.size(1) == 2
        assert u.size(2) == v.size(2)
        assert u.size(3) == v.size(3)
        assert u.size(2) == index_uv.size(2)
        assert u.size(3) == index_uv.size(3)
        # print('UV size:', u.size(), v.size(), index_uv.size(), m.size())
        with torch.no_grad():
            index_uv_img, i_with_dp, bbox_xywh_est, bbox_xywh_gt, index_gt_all, x_norm, y_norm, u_gt_all, v_gt_all, s_gt, m_gt, index_bbox = _extract_single_tensors_from_matches_v2(  # noqa
                proposals_with_gt
            )
        n_batch = len(i_with_dp)

        # NOTE: we need to keep the same computation graph on all the GPUs to
        # perform reduction properly. Hence even if we have no data on one
        # of the GPUs, we still need to generate the computation graph.
        # Add fake (zero) loss in the form Tensor.sum() * 0
        if not n_batch:
            losses["loss_densepose_U"] = u.sum() * 0
            losses["loss_densepose_V"] = v.sum() * 0
            losses["loss_densepose_I"] = index_uv.sum() * 0
            if s is not None:
                losses["loss_densepose_S"] = s.sum() * 0
            if m is not None:
                losses["loss_densepose_M"] = m.sum() * 0
            return losses

        zh = u.size(2)
        zw = u.size(3)

        j_valid, y_lo, y_hi, x_lo, x_hi, w_ylo_xlo, w_ylo_xhi, w_yhi_xlo, w_yhi_xhi = _grid_sampling_utilities(  # noqa
            zh, zw, bbox_xywh_est, bbox_xywh_gt, index_gt_all, x_norm, y_norm, index_bbox
        )

        j_valid_fg = j_valid * (index_gt_all > 0)

        u_gt = u_gt_all[j_valid_fg]
        u_est_all = _extract_at_points_packed(
            u[i_with_dp],
            index_bbox,
            index_gt_all,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        )
        u_est = u_est_all[j_valid_fg]

        v_gt = v_gt_all[j_valid_fg]
        v_est_all = _extract_at_points_packed(
            v[i_with_dp],
            index_bbox,
            index_gt_all,
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo,
            w_ylo_xhi,
            w_yhi_xlo,
            w_yhi_xhi,
        )
        v_est = v_est_all[j_valid_fg]

        index_uv_gt = index_gt_all[j_valid]
        index_uv_est_all = _extract_at_points_packed(
            index_uv[i_with_dp],
            index_bbox,
            slice(None),
            y_lo,
            y_hi,
            x_lo,
            x_hi,
            w_ylo_xlo[:, None],
            w_ylo_xhi[:, None],
            w_yhi_xlo[:, None],
            w_yhi_xhi[:, None],
        )
        index_uv_est = index_uv_est_all[j_valid, :]

        # Resample everything to the estimated data size, no need to resample
        # S_est then:
        if s is not None:
            s_est = s[i_with_dp]

        with torch.no_grad():
            s_gt = _resample_data(
                s_gt.unsqueeze(1),
                bbox_xywh_gt,
                bbox_xywh_est,
                self.heatmap_size,
                self.heatmap_size,
                mode="nearest",
                padding_mode="zeros",
            ).squeeze(1)
        # M_est then
        if m is not None:
            m_est = m[i_with_dp]
        m_gt = s_gt.clamp(min=0, max=1)
        # print('m_gt size:',m_gt.size())

        # add point-based losses:
        u_loss = F.smooth_l1_loss(u_est, u_gt, reduction="sum") * self.w_points
        losses["loss_densepose_U"] = u_loss
        v_loss = F.smooth_l1_loss(v_est, v_gt, reduction="sum") * self.w_points
        losses["loss_densepose_V"] = v_loss
        index_uv_loss = F.cross_entropy(index_uv_est, index_uv_gt.long()) * self.w_part
        losses["loss_densepose_I"] = index_uv_loss

        if s is not None:
            s_loss = F.cross_entropy(s_est, s_gt.long()) * self.w_segm
            losses["loss_densepose_S"] = s_loss
        if m is not None:
            m_loss = F.cross_entropy(m_est, m_gt.long()) * self.w_mask
            losses["loss_densepose_M"] = m_loss
        return losses

def build_ktn_losses(cfg):
    losses = KTNLosses(cfg)
    return losses

def uvToBlocks(u_gt, block_width, block_center, i_est, block_num):
    J = i_est.shape[0]
    block_center = block_center.unsqueeze(0).repeat(J,1,1)
    block_center = block_center[torch.arange(J), i_est, :].squeeze(1)

    block_width = block_width.unsqueeze(0).repeat(J,1,1)
    block_width = block_width[torch.arange(J), i_est, :].squeeze(1) # J*10

    u_gt = u_gt.unsqueeze(1).repeat(1,block_num) # J*10

    offsets = (u_gt - block_center)/block_width # J*10
    offsets_val, offsets_indices = torch.topk(torch.abs(offsets), 1, dim=1, largest=False, sorted=True) #J*2
    gt_cls = offsets_indices[torch.arange(J),0].squeeze() # J
    gt_offsets = offsets[torch.arange(J), gt_cls]
    
    return gt_cls,gt_offsets
