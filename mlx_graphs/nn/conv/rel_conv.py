from typing import Any, Optional, Tuple

import mlx.core as mx
from mlx import nn

from mlx_graphs.nn.linear import Linear
from mlx_graphs.nn.message_passing import MessagePassing
from mlx_graphs.utils import degree, scatter


class GeneralizedRelationalConv(MessagePassing):
    r"""Generalized relational convolution layer from
    `"Neural Bellman-Ford Networks: A General Graph Neural Network Framework
      for Link Prediction" <https://arxiv.org/abs/2106.06935>`_ paper.

    Adopted from the PyG version from here:
    `https://github.com/KiddoZhu/NBFNet-PyG/blob/master/nbfnet/layers.py`

    Part of the Neural Bellman-Ford networks (NBFNet) holding
    state-of-the-art in KG completion.
    Works with multi-relational graphs where edge types are stored in `edge_labels`.
    The message function composes node and relation vectors in three possible ways.
    The expected behavior is to work with "labeling trick" graphs where one node
    in the graph is labeled with a `query` vector, while rest are zeros.
    Message passing is then done separately for each data point in the batch.
    The input shape is expected to be [batch_size, num_nodes, input_dim]

    Alternatively, the layer can work as a standard relational conv
    with shapes [num_nodes, input_dim].

    Note that this implementation materializes all edge messages and is O(E).
    The complexity can be further reduced by adopting the O(V) `rspmm` C++ kernel
    from the NBFNet-PyG repo to the MLX framework (not implemented here).

    Args:
        input_dim: input feature dimension (same for node and edge features)
        output_dim: output node feature dimension
        num_relation: number of unique relations in the graph
        message_func: "transe" (sum), "distmult" (mult),
            "rotate" (complex rotation). Default: ``distmult``
        aggregate_func: "add", "mean", or "pna". Default: ``add``
        layer_norm: whether to use layer norm
            (often crucial to the performance). Default: ``True``
        activation: non-linearity. Default: ``relu``
        dependent: whether to use separate relation embedding matrix
             ``False`` or build relations from the input relations ``True``
        node_dim: for 3D batches, specified which dimension contains all nodes.
            Default: ``0``

    Example:

    .. code-block:: python

        import mlx.core as mx
        import mlx.nn as nn
        from mlx_graphs.nn import GeneralizedRelationalConv

        input_dim = 16
        output_dim = 16
        num_relations = 3

        conv = GeneralizedRelationalConv(input_dim, output_dim, num_relations)

        batch_size = 2
        edge_index = mx.array([[0, 1, 2, 3, 4], [0, 0, 1, 1, 3]])
        edge_types = mx.array([0, 0, 1, 1, 2])
        boundary = mx.random.uniform(0, 1, shape=(batch_size, 5, 16)
        size = (node_features.shape[0], node_features.shape[0])

        layer_input = boundary
        h = conv(layer_input, query, boundary, edge_index, edge_type, size)

        # optional: residual connection if input dim == output dim
        h = h + layer_input
        layer_input = h

    """

    eps = 1e-6

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        num_relation: int,
        message_func: str = "distmult",
        aggregate_func: str = "add",
        layer_norm: bool = True,
        activation: str = "relu",
        dependent: bool = False,
        node_dim: int = 0,
        **kwargs,
    ):
        kwargs.setdefault("aggr", "add")
        super(GeneralizedRelationalConv, self).__init__(**kwargs)

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.num_relation = num_relation
        self.message_func = message_func
        self.aggregate_func = aggregate_func
        self.dependent = dependent
        self.node_dim = node_dim

        if layer_norm:
            self.layer_norm = nn.LayerNorm(output_dim)
        else:
            self.layer_norm = None

        if isinstance(activation, str):
            self.activation = getattr(nn, activation)
        else:
            self.activation = activation

        if self.aggregate_func == "pna":
            # 9 for 3 aggregations (mean, max, std)
            # and 3 scalers (identity, degree, 1/degree)
            # +1 for the old state, so 10 is the final multiplier
            self.linear = Linear(input_dim * 10, output_dim)
        else:
            self.linear = Linear(input_dim * 2, output_dim)

        if dependent:
            # obtain relation embeddings as a projection of the query relation
            self.relation_linear = Linear(input_dim, num_relation * input_dim)
        else:
            # relation embeddings as an independent embedding matrix per each layer
            self.relation = nn.Embedding(num_relation, input_dim)

    def __call__(
        self,
        node_features: mx.array,
        edge_index: mx.array,
        edge_type: mx.array,
        boundary: mx.array,
        query: Optional[mx.array] = None,
        size: Tuple[int, int] = None,
        edge_weight: mx.array = None,
        **kwargs: Any,
    ) -> mx.array:
        """Computes the forward pass of GeneralizedRelationalConv.

        Args:
            node_features: Input node features,
                shape `[bs, num_nodes, dim]` or `[num_nodes, dim]`
            edge_index: Input edge index of shape `[2, num_edges]`
            edge_type: Input edge types of shape `[num_edges,]`
            boundary: Initial node feats `[bs, num_nodes, dim]` or `[num_nodes, dim]`
            query: Optional input node queries, shape `[bs, dim]`
            size: a tuple encoding the size of the graph eg `(5, 5)`
            edge_weights: Edge weights leveraged in message passing. Default: ``None``

        Returns:
            The computed node embeddings
        """
        self.input_dims = len(node_features.shape)

        batch_size = node_features.shape[0] if self.input_dims == 3 else 1
        if size is None:
            num_nodes = (
                node_features.shape[0]
                if self.input_dims == 2
                else node_features.shape[1]
            )
            size = (num_nodes, num_nodes)

        # input: (bs, num_nodes, dim)
        if self.dependent:
            assert query is not None, "query must be supplied when dependent=True"
            assert (
                self.input_dims == 3
            ), "expected input shape is [batch_size, num_nodes, dim]"
            # relation features as a projection of input "query" (relation) embeddings
            relation = self.relation_linear(query).reshape(
                batch_size, self.num_relation, self.input_dim
            )
        else:
            # relation features as an embedding matrix unique to each layer
            # relation: (batch_size, num_relation, dim)
            relation = mx.repeat(self.relation.weight[None, :], batch_size, axis=0)
            # if self.input_dims == 2:
            #     relation = relation.squeeze(0)

        if edge_weight is None:
            edge_weight = mx.ones(len(edge_type))

        # since mlx_graphs gathers always along dimension 0 (num_nodes are rows)
        # we have to reshape input features accordingly
        if self.input_dims == 3:
            node_features = node_features.transpose(1, 0, 2)
            boundary = boundary.transpose(1, 0, 2)

        # note that we send the initial boundary condition (node states at layer0)
        # to the message passing
        # correspond to Eq.6 on p5 in https://arxiv.org/pdf/2106.06935.pdf
        output = self.propagate(
            node_features=node_features,
            edge_index=edge_index,
            message_kwargs=dict(
                relation=relation, boundary=boundary, edge_type=edge_type
            ),
            aggregate_kwargs=dict(edge_weight=edge_weight, dim_size=size),
            update_kwargs=dict(input=node_features),
        )
        return output

    def message(
        self,
        src_features: mx.array,
        dst_features: mx.array,
        relation: mx.array,
        boundary: mx.array,
        edge_type: mx.array,
    ) -> mx.array:
        # if self.input_dims =
        # extracting relation features
        relation_j = relation[:, edge_type]

        if self.input_dims == 3:
            relation_j = relation_j.transpose(1, 0, 2)
        else:
            relation_j = relation_j.squeeze(0)

        if self.message_func == "transe":
            message = src_features + relation_j
        elif self.message_func == "distmult":
            message = src_features * relation_j
        elif self.message_func == "rotate":
            x_j_re, x_j_im = src_features.chunk(2, dim=-1)
            r_j_re, r_j_im = relation_j.chunk(2, dim=-1)
            message_re = x_j_re * r_j_re - x_j_im * r_j_im
            message_im = x_j_re * r_j_im + x_j_im * r_j_re
            message = mx.concatenate([message_re, message_im], axis=-1)
        else:
            raise ValueError("Unknown message function `%s`" % self.message_func)

        # augment messages with the boundary condition
        message = mx.concatenate(
            [message, boundary], axis=0
        )  # (num_edges + num_nodes, batch_size, input_dim)

        return message

    def aggregate(self, messages, indices, edge_weight, dim_size) -> mx.array:
        # augment aggregation index with self-loops for the boundary condition
        index = mx.concatenate(
            [indices, mx.arange(dim_size[0])]
        )  # (num_edges + num_nodes,)
        edge_weight = mx.concatenate([edge_weight, mx.ones(dim_size[0])])
        shape = [1] * messages.ndim
        shape[self.node_dim] = -1
        edge_weight = edge_weight.reshape(shape)

        if self.aggregate_func == "pna":
            mean = scatter(
                messages * edge_weight,
                index,
                axis=self.node_dim,
                out_size=dim_size[0],
                aggr="mean",
            )
            sq_mean = scatter(
                messages**2 * edge_weight,
                index,
                axis=self.node_dim,
                out_size=dim_size[0],
                aggr="mean",
            )
            max = scatter(
                messages * edge_weight,
                index,
                axis=self.node_dim,
                out_size=dim_size[0],
                aggr="max",
            )
            # scatter_min is not implemented in MLX-graphs
            # min = scatter(
            #     messages * edge_weight,
            #     index, axis=self.node_dim,
            #     out_size=dim_size[0],
            #     aggr="min"
            # )
            std = mx.clip(sq_mean - mean**2, a_min=self.eps, a_max=None).sqrt()
            features = mx.concatenate(
                [mean[..., None], max[..., None], std[..., None]], axis=-1
            )
            features = features.flatten(-2)
            if self.input_dims == 2:
                features = features[:, None, :]
            degree_out = degree(index, dim_size[0])[..., None, None]
            scale = degree_out.log()
            scale = scale / scale.mean()
            scales = mx.concatenate(
                [
                    mx.ones_like(scale),
                    scale,
                    1 / mx.clip(scale, a_min=1e-2, a_max=None),
                ],
                axis=-1,
            )
            output = (features[..., None] * scales[:, :, None, :]).flatten(-2)

            if self.input_dims == 2:
                output = output.squeeze(1)
        else:
            output = scatter(
                messages * edge_weight,
                index,
                axis=self.node_dim,
                out_size=dim_size[0],
                aggr=self.aggregate_func,
            )

        return output

    def update_nodes(
        self,
        aggregated: mx.array,
        old: mx.array,
    ) -> mx.array:
        # node update: a function of old states (old) and layer's output (aggregated)
        output = self.linear(mx.concatenate([old, aggregated], axis=-1))
        if self.layer_norm:
            output = self.layer_norm(output)
        if self.activation:
            output = self.activation(output)
        return output.transpose(1, 0, 2) if self.input_dims == 3 else output
