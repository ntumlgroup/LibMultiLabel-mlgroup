from __future__ import annotations
import hashlib

import logging
import pathlib
import pickle
import uuid
from typing import Callable

import blinkless.sparse
import blinkless.stack_impl
import numpy as np
import scipy.sparse as sparse
import sklearn.cluster
import sklearn.preprocessing
from tqdm import tqdm

from . import fileio, linear

__all__ = ["train_tree"]


class Node:
    def __init__(
        self,
        label_map: np.ndarray,
        children: list[Node],
    ):
        """
        Args:
            label_map (np.ndarray): The labels under this node.
            children (list[Node]): Children of this node. Must be an empty list if this is a leaf node.
        """
        self.label_map = label_map
        self.children = children

    def isLeaf(self) -> bool:
        return len(self.children) == 0

    def dfs(self, visit: Callable[[Node], None]):
        visit(self)
        # Stops if self.children is empty, i.e. self is a leaf node
        for child in self.children:
            child.dfs(visit)


class TreeModel:
    def __init__(
        self,
        root: Node,
        flat_model: linear.FlatModel,
        weight_map: np.ndarray,
        mmap_root: pathlib.Path,
    ):
        self.name = "mmap-tree"
        self.root = root
        self.flat_model = flat_model
        self.weight_map = weight_map
        self.mmap = {
            "root": mmap_root,
            "shape": flat_model.weights.shape,
            "nnz": flat_model.weights.nnz,
            "dtype": flat_model.weights.data.dtype,
        }

    def predict_values(
        self,
        x: sparse.csr_matrix,
        beam_width: int = 10,
    ) -> np.ndarray:
        """Calculates the decision values associated with x.

        Args:
            x (sparse.csr_matrix): A matrix with dimension number of instances * number of features.
            beam_width (int, optional): Number of candidates considered during beam search. Defaults to 10.

        Returns:
            np.ndarray: A matrix with dimension number of instances * number of classes.
        """
        # number of instances * number of labels + total number of metalabels
        all_preds = linear.predict_values(self.flat_model, x)
        return np.vstack([self._beam_search(all_preds[i], beam_width) for i in range(all_preds.shape[0])])

    def _beam_search(self, instance_preds: np.ndarray, beam_width: int) -> np.ndarray:
        """Predict with beam search using cached decision values for a single instance.

        Args:
            instance_preds (np.ndarray): A vector of cached decision values of each node, has dimension number of labels + total number of metalabels.
            beam_width (int): Number of candidates considered.

        Returns:
            np.ndarray: A vector with dimension number of classes.
        """
        cur_level = [(self.root, 0.0)]  # pairs of (node, score)
        next_level = []
        while True:
            num_internal = sum(map(lambda pair: not pair[0].isLeaf(), cur_level))
            if num_internal == 0:
                break

            for node, score in cur_level:
                if node.isLeaf():
                    next_level.append((node, score))
                    continue
                slice = np.s_[self.weight_map[node.index] : self.weight_map[node.index + 1]]
                pred = instance_preds[slice]
                children_score = score - np.maximum(0, 1 - pred) ** 2
                next_level.extend(zip(node.children, children_score.tolist()))

            cur_level = sorted(next_level, key=lambda pair: -pair[1])[:beam_width]
            next_level = []

        num_labels = len(self.root.label_map)
        scores = np.full(num_labels, -np.inf)
        for node, score in cur_level:
            slice = np.s_[self.weight_map[node.index] : self.weight_map[node.index + 1]]
            pred = instance_preds[slice]
            scores[node.label_map] = np.exp(score - np.maximum(0, 1 - pred) ** 2)
        return scores


def train_tree(
    y: sparse.csr_matrix,
    x: sparse.csr_matrix,
    options: str = "",
    K=100,
    dmax=10,
    verbose: bool = True,
) -> TreeModel:
    """Trains a linear model for multiabel data using a divide-and-conquer strategy.
    The algorithm used is based on https://github.com/xmc-aalto/bonsai.

    Args:
        y (sparse.csr_matrix): A 0/1 matrix with dimensions number of instances * number of classes.
        x (sparse.csr_matrix): A matrix with dimensions number of instances * number of features.
        options (str): The option string passed to liblinear.
        K (int, optional): Maximum degree of nodes in the tree. Defaults to 100.
        dmax (int, optional): Maximum depth of the tree. Defaults to 10.
        verbose (bool, optional): Output extra progress information. Defaults to True.

    Returns:
        A model which can be used in predict_values.
    """
    random_state = np.random.get_state()[1]
    tree_fingerprint = str((*y.shape, *x.shape, y.nnz, x.nnz, random_state, x[0].data, x[-1].data))
    h = hashlib.sha256()
    h.update(tree_fingerprint.encode("utf-8"))
    cache_path = pathlib.Path(f"tree_cache/{h.hexdigest()[:32]}.pickle")

    if cache_path.is_file():
        logging.info(f'loading tree (no weights) from cache "{cache_path}"')
        with open(cache_path, "rb") as f:
            root = pickle.load(f)
    else:
        label_representation = (y.T * x).tocsr()
        label_representation = sklearn.preprocessing.normalize(label_representation, norm="l2", axis=1)

        root = _build_tree(label_representation, np.arange(y.shape[1]), 0, K, dmax)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(root, f)

    num_nodes = 0

    def count(node):
        nonlocal num_nodes
        num_nodes += 1

    root.dfs(count)

    mmap_root = pathlib.Path("weights") / uuid.uuid4().hex
    mmap_root.mkdir(parents=True, exist_ok=True)
    pbar = tqdm(total=num_nodes, disable=not verbose)

    has_bias = "-B" in options

    chunk_size = 4096
    data = fileio.Array(
        mmap_root / "nodes.data",
        shape=chunk_size,
        dtype=np.float64,
    )
    indices = fileio.Array(
        mmap_root / "nodes.indices",
        shape=chunk_size,
        dtype=np.int32,
    )
    indptr = fileio.Array(
        mmap_root / "nodes.indptr",
        shape=num_nodes * (x.shape[1] + has_bias + 1),
        dtype=np.int32,
    )
    mmap_info = {
        "num_data": 0,
        "num_indptr": 0,
        "chunk_size": chunk_size,
        "data": data,
        "indices": indices,
        "indptr": indptr,
    }

    def visit(node):
        relevant_instances = y[:, node.label_map].getnnz(axis=1) > 0
        _train_node(y[relevant_instances], x[relevant_instances], options, node, mmap_info)
        pbar.update()

    root.dfs(visit)
    pbar.close()

    flat_model, weight_map = _flatten_model(root, mmap_root / "flat_model")
    return TreeModel(root, flat_model, weight_map, mmap_root)


def _build_tree(
    label_representation: sparse.csr_matrix,
    label_map: np.ndarray,
    d: int,
    K: int,
    dmax: int,
) -> Node:
    """Builds the tree recursively by kmeans clustering.

    Args:
        label_representation (sparse.csr_matrix): A matrix with dimensions number of classes under this node * number of features.
        label_map (np.ndarray): Maps 0..label_representation.shape[0] to the original label indices.
        d (int): Current depth.
        K (int): Maximum degree of nodes in the tree.
        dmax (int): Maximum depth of the tree.

    Returns:
        Node: root of the (sub)tree built from label_representation.
    """
    if d >= dmax or label_representation.shape[0] <= K:
        return Node(label_map=label_map, children=[])

    metalabels = (
        sklearn.cluster.KMeans(
            K,
            random_state=np.random.randint(2**31 - 1),
            n_init=1,
            max_iter=300,
            tol=0.0001,
            algorithm="elkan",
        )
        .fit(label_representation)
        .labels_
    )

    children = []
    for i in range(K):
        child_representation = label_representation[metalabels == i]
        child_map = label_map[metalabels == i]
        child = _build_tree(child_representation, child_map, d + 1, K, dmax)
        children.append(child)

    return Node(label_map=label_map, children=children)


def _train_node(
    y: sparse.csr_matrix,
    x: sparse.csr_matrix,
    options: str,
    node: Node,
    mmap_info: dict,
):
    """If node is internal, computes the metalabels representing each child and trains
    on the metalabels. Otherwise, train on y.

    Args:
        y (sparse.csr_matrix): A 0/1 matrix with dimensions number of instances * number of classes.
        x (sparse.csr_matrix): A matrix with dimensions number of instances * number of features.
        options (str): The option string passed to liblinear.
        node (Node): Node to be trained.
    """
    if node.isLeaf():
        node.model = linear.train_1vsrest(y[:, node.label_map], x, options, False)
    else:
        # meta_y[i, j] is 1 if the ith instance is relevant to the jth child.
        # getnnz returns an ndarray of shape number of instances.
        # This must be reshaped into number of instances * 1 to be interpreted as a column.
        meta_y = [y[:, child.label_map].getnnz(axis=1)[:, np.newaxis] > 0 for child in node.children]
        meta_y = sparse.csr_matrix(np.hstack(meta_y))
        node.model = linear.train_1vsrest(meta_y, x, options, False)

    node.model.weights = _as_mmap(
        sparse.csr_matrix(node.model.weights),
        mmap_info,
    )


def _flatten_model(root: Node, mmap_path: pathlib.Path) -> tuple[linear.FlatModel, np.ndarray]:
    """Flattens tree weight matrices into a single weight matrix. The flattened weight
    matrix is used to predict all possible values, which is cached for beam search.
    This pessimizes complexity but is faster in practice.
    Consecutive values of the returned map denotes the start and end indices of the
    weights of each node. Conceptually, given root and node:
        flat_model, weight_map = _flatten_model(root)
        slice = np.s_[weight_map[node.index]:
                      weight_map[node.index+1]]
        node.model.weights == flat_model.weights[:, slice]

    Args:
        root (Node): Root of the tree.

    Returns:
        tuple[linear.FlatModel, np.ndarray]: The flattened model and the ranges of each node.
    """
    index = 0
    weights = []
    bias = root.model.bias

    def visit(node):
        assert bias == node.model.bias
        nonlocal index
        node.index = index
        index += 1
        weights.append(node.model.__dict__.pop("weights"))

    root.dfs(visit)

    model = linear.FlatModel(
        name="flattened-tree",
        weights=_mmap_hstack(weights, mmap_path),
        bias=bias,
        thresholds=0,
    )

    # w.shape[1] is the number of labels/metalabels of each node
    weight_map = np.cumsum([0] + list(map(lambda w: w.shape[1], weights)))

    return model, weight_map


class dummy:
    """Dummy class used to skip unnecessary checks on csr_matrix."""

    def __init__(self, triplet, shape):
        self.data = triplet[0]
        self.indices = triplet[1]
        self.indptr = triplet[2]
        self.shape = shape
        self.nnz = self.data.size

    @staticmethod
    def csr_matrix(triplet: tuple, shape: tuple[int, int]):
        return dummy(triplet, shape)


def _as_mmap(
    arr: sparse.csr_matrix,
    mmap_info: dict,
) -> sparse.csr_matrix:
    nnz = arr.nnz
    num_data = mmap_info["num_data"]
    num_indptr = mmap_info["num_indptr"]
    mm_data = mmap_info["data"]
    mm_indices = mmap_info["indices"]
    mm_indptr = mmap_info["indptr"]
    buffer_size = mm_data.shape[0]

    if buffer_size < num_data + nnz:
        buffer_size = 2 << (num_data + nnz - 1).bit_length()
        mm_data.resize(buffer_size)
        mm_indices.resize(buffer_size)

    data = mm_data[num_data : num_data + nnz]
    indices = mm_indices[num_data : num_data + nnz]
    indptr = mm_indptr[num_indptr : num_indptr + arr.shape[0] + 1]

    data[:] = arr.data
    indices[:] = arr.indices
    indptr[:] = arr.indptr

    mmap_info["num_data"] += nnz
    mmap_info["num_indptr"] += arr.shape[0] + 1

    return dummy.csr_matrix((data, indices, indptr), shape=arr.shape)


def _mmap_hstack(blocks: list[sparse.csr_matrix], prefix: pathlib.Path) -> sparse.csr_matrix:
    if len(blocks) == 0:
        return sparse.csr_matrix((0, 0))

    info = _hstack_info(blocks)

    data = fileio.Array(f"{prefix}.data", dtype=info["dtype"], shape=info["nnz"])
    indices = fileio.Array(f"{prefix}.indices", dtype=np.int32, shape=info["nnz"])
    indptr = fileio.Array(f"{prefix}.indptr", dtype=np.int64, shape=info["m"] + 1)

    blinkless.stack_impl.hstack_rr_to(
        info["m"],
        info["cols_array"],
        info["data_list"],
        info["indices_list"],
        info["indptr_list"],
        data.view,
        indices.view,
        indptr.view,
    )

    return sparse.csr_matrix((data.view, indices.view, indptr.view), shape=(info["m"], info["n"]))


def _hstack_info(blocks: list[sparse.csr_matrix]):
    """Copy of blinkless.sparse._check_hstack_rr except without checks."""
    m = blocks[0].shape[0]
    n = 0
    nnz = 0
    cols_list = []
    data_list = []
    indices_list = []
    indptr_list = []
    dtypes_list = []
    for block in blocks:
        if block.shape[0] != m:
            raise ValueError("all the input matrix dimensions for the concatenation" "axis must match exactly")
        n = n + block.shape[1]
        nnz = nnz + block.nnz
        cols_list.append(block.shape[1])
        data_list.append(block.data)
        indices_list.append(block.indices)
        indptr_list.append(block.indptr)
        dtypes_list.append(block.data.dtype)

    dtype = np.find_common_type(dtypes_list, [])

    return {
        "m": m,
        "n": n,
        "nnz": nnz,
        "cols_array": np.array(cols_list),
        "data_list": data_list,
        "indices_list": indices_list,
        "indptr_list": indptr_list,
        "dtype": dtype,
    }
