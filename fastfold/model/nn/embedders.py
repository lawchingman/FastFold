# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
from typing import Tuple, Dict

from functools import partial
from fastfold.utils import all_atom_multimer
from fastfold.utils.feats import (
    build_template_angle_feat,
    build_template_pair_feat,
)
from fastfold.model.nn.primitives import Linear, LayerNorm
from fastfold.utils.tensor_utils import one_hot
from fastfold.model.fastnn.ops import RecyclingEmbedder
from fastfold.model.nn.template import (
    TemplatePairStack,
    TemplatePointwiseAttention,
)
from fastfold.utils import geometry
from fastfold.utils.tensor_utils import one_hot, tensor_tree_map, dict_multimap

class InputEmbedder(nn.Module):
    """
    Embeds a subset of the input features.

    Implements Algorithms 3 (InputEmbedder) and 4 (relpos).
    """

    def __init__(
        self,
        tf_dim: int,
        msa_dim: int,
        c_z: int,
        c_m: int,
        relpos_k: int,
        **kwargs,
    ):
        """
        Args:
            tf_dim:
                Final dimension of the target features
            msa_dim:
                Final dimension of the MSA features
            c_z:
                Pair embedding dimension
            c_m:
                MSA embedding dimension
            relpos_k:
                Window size used in relative positional encoding
        """
        super(InputEmbedder, self).__init__()

        self.tf_dim = tf_dim
        self.msa_dim = msa_dim

        self.c_z = c_z
        self.c_m = c_m

        self.linear_tf_z_i = Linear(tf_dim, c_z)
        self.linear_tf_z_j = Linear(tf_dim, c_z)
        self.linear_tf_m = Linear(tf_dim, c_m)
        self.linear_msa_m = Linear(msa_dim, c_m)

        # RPE stuff
        self.relpos_k = relpos_k
        self.no_bins = 2 * relpos_k + 1
        self.linear_relpos = Linear(self.no_bins, c_z)

    def relpos(self, ri: torch.Tensor):
        """
        Computes relative positional encodings

        Implements Algorithm 4.

        Args:
            ri:
                "residue_index" features of shape [*, N]
        """
        d = ri[..., None] - ri[..., None, :]
        boundaries = torch.arange(
            start=-self.relpos_k, end=self.relpos_k + 1, device=d.device
        )
        oh = one_hot(d, boundaries).type(ri.dtype)
        return self.linear_relpos(oh)

    def forward(
        self,
        tf: torch.Tensor,
        ri: torch.Tensor,
        msa: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Args:
            tf:
                "target_feat" features of shape [*, N_res, tf_dim]
            ri:
                "residue_index" features of shape [*, N_res]
            msa:
                "msa_feat" features of shape [*, N_clust, N_res, msa_dim]
        Returns:
            msa_emb:
                [*, N_clust, N_res, C_m] MSA embedding
            pair_emb:
                [*, N_res, N_res, C_z] pair embedding

        """
        # [*, N_res, c_z]
        tf_emb_i = self.linear_tf_z_i(tf)
        tf_emb_j = self.linear_tf_z_j(tf)

        # [*, N_res, N_res, c_z]
        pair_emb = self.relpos(ri.type(tf_emb_i.dtype))
        pair_emb += tf_emb_i[..., None, :] + tf_emb_j[..., None, :, :]

        # [*, N_clust, N_res, c_m]
        n_clust = msa.shape[-3]
        tf_m = (
            self.linear_tf_m(tf)
            .unsqueeze(-3)
            .expand(((-1,) * len(tf.shape[:-2]) + (n_clust, -1, -1)))
        )
        msa_emb = self.linear_msa_m(msa) + tf_m

        return msa_emb, pair_emb


class TemplateEmbedder(nn.Module):
    def __init__(self, config):
        super(TemplateEmbedder, self).__init__()
        
        self.config = config
        self.template_angle_embedder = TemplateAngleEmbedder(
            **config["template_angle_embedder"],
        )
        self.template_pair_embedder = TemplatePairEmbedder(
            **config["template_pair_embedder"],
        )
        self.template_pair_stack = TemplatePairStack(
            **config["template_pair_stack"],
        )
        self.template_pointwise_att = TemplatePointwiseAttention(
            **config["template_pointwise_attention"],
        )
    
    def forward(self, 
        batch, 
        z, 
        pair_mask, 
        templ_dim, 
        chunk_size, 
        _mask_trans=True,
        inplace=False
    ):
        # Embed the templates one at a time (with a poor man's vmap)
        template_embeds = []
        n_templ = batch["template_aatype"].shape[templ_dim]

        if isinstance(chunk_size, int) and 1 <= chunk_size <= 4:
            t = torch.empty((n_templ, z.shape[0], z.shape[1], 64), dtype=z.dtype, device='cpu')
        else:
            t = torch.empty((n_templ, z.shape[0], z.shape[1], 64), dtype=z.dtype, device=z.device)

        for i in range(n_templ):
            idx = batch["template_aatype"].new_tensor(i)
            single_template_feats = tensor_tree_map(
                lambda t: torch.index_select(t, templ_dim, idx),
                batch,
            )

            single_template_embeds = {}
            if self.config.embed_angles:
                template_angle_feat = build_template_angle_feat(
                    single_template_feats,
                )

                # [*, S_t, N, C_m]
                a = self.template_angle_embedder(template_angle_feat)

                single_template_embeds["angle"] = a

            # [*, S_t, N, N, C_t]
            tt = build_template_pair_feat(
                single_template_feats,
                use_unit_vector=self.config.use_unit_vector,
                inf=self.config.inf,
                chunk=chunk_size,
                eps=self.config.eps,
                **self.config.distogram,
            ).to(z.dtype).to(z.device)

            tt = self.template_pair_embedder(tt)
            # single_template_embeds.update({"pair": t})

            template_embeds.append(single_template_embeds)

            # [*, S_t, N, N, C_z]
            if inplace:
                tt = [tt]
                t[i] = self.template_pair_stack.inplace(
                    tt, 
                    pair_mask.unsqueeze(-3).to(dtype=z.dtype), 
                    chunk_size=chunk_size,
                    _mask_trans=_mask_trans,
                )[0].to(t.device)
            else:
                t[i] = self.template_pair_stack(
                    tt, 
                    pair_mask.unsqueeze(-3).to(dtype=z.dtype), 
                    chunk_size=chunk_size,
                    _mask_trans=_mask_trans,
                ).to(t.device)

        del tt, single_template_feats

        template_embeds = dict_multimap(
            partial(torch.cat, dim=templ_dim),
            template_embeds,
        )

        # [*, N, N, C_z]
        z = self.template_pointwise_att(
            t,
            z,
            template_mask=batch["template_mask"].to(dtype=z.dtype),
            chunk_size=chunk_size * 256 if chunk_size is not None else chunk_size,
        )

        ret = {}
        if self.config.embed_angles:
            ret["template_single_embedding"] = template_embeds["angle"]

        return ret, z


class TemplateAngleEmbedder(nn.Module):
    """
    Embeds the "template_angle_feat" feature.

    Implements Algorithm 2, line 7.
    """

    def __init__(
        self,
        c_in: int,
        c_out: int,
        **kwargs,
    ):
        """
        Args:
            c_in:
                Final dimension of "template_angle_feat"
            c_out:
                Output channel dimension
        """
        super(TemplateAngleEmbedder, self).__init__()

        self.c_out = c_out
        self.c_in = c_in

        self.linear_1 = Linear(self.c_in, self.c_out, init="relu")
        self.relu = nn.ReLU()
        self.linear_2 = Linear(self.c_out, self.c_out, init="relu")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: [*, N_templ, N_res, c_in] "template_angle_feat" features
        Returns:
            x: [*, N_templ, N_res, C_out] embedding
        """
        x = self.linear_1(x)
        x = self.relu(x)
        x = self.linear_2(x)

        return x


class TemplatePairEmbedder(nn.Module):
    """
    Embeds "template_pair_feat" features.

    Implements Algorithm 2, line 9.
    """

    def __init__(
        self,
        c_in: int,
        c_out: int,
        **kwargs,
    ):
        """
        Args:
            c_in:

            c_out:
                Output channel dimension
        """
        super(TemplatePairEmbedder, self).__init__()

        self.c_in = c_in
        self.c_out = c_out

        # Despite there being no relu nearby, the source uses that initializer
        self.linear = Linear(self.c_in, self.c_out, init="relu")

    def forward(
        self,
        x: torch.Tensor,
    ) -> torch.Tensor:
        """
        Args:
            x:
                [*, C_in] input tensor
        Returns:
            [*, C_out] output tensor
        """
        x = self.linear(x)

        return x


class ExtraMSAEmbedder(nn.Module):
    """
    Embeds unclustered MSA sequences.

    Implements Algorithm 2, line 15
    """

    def __init__(
        self,
        c_in: int,
        c_out: int,
        **kwargs,
    ):
        """
        Args:
            c_in:
                Input channel dimension
            c_out:
                Output channel dimension
        """
        super(ExtraMSAEmbedder, self).__init__()

        self.c_in = c_in
        self.c_out = c_out

        self.linear = Linear(self.c_in, self.c_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x:
                [*, N_extra_seq, N_res, C_in] "extra_msa_feat" features
        Returns:
            [*, N_extra_seq, N_res, C_out] embedding
        """
        x = self.linear(x)

        return x