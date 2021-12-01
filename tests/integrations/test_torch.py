import wandb
import pytest
import sys
from typing import List, Tuple

from collections import Counter
from itertools import combinations_with_replacement

# Numbers within a factor of roughly 1e8 of the maximum value supported
# by 32 and 64 bit floats. torch.linspace and torch.histc are both prone
# to crashing when they encounter numbers much beyond this magnitude.
HIGH32 = 1e30
HIGH64 = 1e300

# The machine epsilon, or smallest relative difference between any two
# numbers, for 32 and 64 bit floats. Equivalent to torch.finfo(dtype).eps
# in pytorch v1.0.0 and later.
EPS32 = float.fromhex("0x1p-23")
EPS64 = float.fromhex("0x1p-52")

# The smallest "normal number" for 32 and 64 bit floats. Equivalent to
# torch.finfo(dtype).tiny in pytorch v1.0.0 and later.
TINY32 = float.fromhex("0x1p-126")
TINY64 = float.fromhex("0x1p-1022")

if sys.version_info >= (3, 9):
    pytest.importorskip("pytorch", reason="pytorch doesnt support py3.9 yet")

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except ImportError:

    class nn:
        Module = object


pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 5), reason="PyTorch no longer supports py2"
)


def dummy_torch_tensor(size, requires_grad=True):
    return torch.ones(size, requires_grad=requires_grad)


class DynamicModule(nn.Module):
    def __init__(self):
        super(DynamicModule, self).__init__()
        self.choices = nn.ModuleDict(
            {"conv": nn.Conv2d(10, 10, 3), "pool": nn.MaxPool2d(3)}
        )
        self.activations = nn.ModuleDict(
            [["lrelu", nn.LeakyReLU()], ["prelu", nn.PReLU()]]
        )

    def forward(self, x, choice, act):
        x = self.choices[choice](x)
        x = self.activations[act](x)
        return x


class EmbModel(nn.Module):
    def __init__(self, x=16, y=32):
        super().__init__()
        self.emb1 = nn.Embedding(x, y)
        self.emb2 = nn.Embedding(x, y)

    def forward(self, x):
        return {"key": {"emb1": self.emb1(x), "emb2": self.emb2(x),}}


class EmbModelWrapper(nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.emb = EmbModel(*args, **kwargs)

    def forward(self, *args, **kwargs):
        return self.emb(*args, **kwargs)


class Discrete(nn.Module):
    def __init__(self):
        super(Discrete, self).__init__()

    def forward(self, x):
        return nn.functional.softmax(x, dim=0)


class DiscreteModel(nn.Module):
    def __init__(self, num_outputs=2):
        super(DiscreteModel, self).__init__()
        self.linear1 = nn.Linear(1, 10)
        self.linear2 = nn.Linear(10, num_outputs)
        self.dist = Discrete()

    def forward(self, x):
        x = self.linear1(x)
        x = self.linear2(x)
        return self.dist(x)


class ParameterModule(nn.Module):
    def __init__(self):
        super(ParameterModule, self).__init__()
        self.params = nn.ParameterList(
            [nn.Parameter(torch.ones(10, 10)) for i in range(10)]
        )
        self.otherparam = nn.Parameter(torch.Tensor(5))

    def forward(self, x):
        # ParameterList can act as an iterable, or be indexed using ints
        for i, p in enumerate(self.params):
            x = self.params[i // 2].mm(x) + p.mm(x)
        return x


class Sequence(nn.Module):
    def __init__(self):
        super(Sequence, self).__init__()
        self.lstm1 = nn.LSTMCell(1, 51)
        self.lstm2 = nn.LSTMCell(51, 51)
        self.linear = nn.Linear(51, 1)

    def forward(self, input, future=0):
        outputs = []
        h_t = dummy_torch_tensor((input.size(0), 51))
        c_t = dummy_torch_tensor((input.size(0), 51))
        h_t2 = dummy_torch_tensor((input.size(0), 51))
        c_t2 = dummy_torch_tensor((input.size(0), 51))

        for i, input_t in enumerate(input.chunk(input.size(1), dim=1)):
            h_t, c_t = self.lstm1(input_t, (h_t, c_t))
            h_t2, c_t2 = self.lstm2(h_t, (h_t2, c_t2))
            output = self.linear(h_t2)
            outputs += [output]
        for i in range(future):  # if we should predict the future
            h_t, c_t = self.lstm1(output, (h_t, c_t))
            h_t2, c_t2 = self.lstm2(h_t, (h_t2, c_t2))
            output = self.linear(h_t2)
            outputs += [output]
        outputs = torch.stack(outputs, 1).squeeze(2)
        return outputs


class ConvNet(nn.Module):
    def __init__(self):
        super(ConvNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, kernel_size=5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)


def init_conv_weights(layer, weights_std=0.01, bias=0):
    """Initialize weights for subnet convolution"""
    nn.init.normal_(layer.weight.data, std=weights_std)
    nn.init.constant_(layer.bias.data, val=bias)
    return layer


def conv3x3(in_channels, out_channels, **kwargs):
    """Return a 3x3 convolutional layer for SubNet"""
    layer = nn.Conv2d(in_channels, out_channels, kernel_size=3, **kwargs)
    layer = init_conv_weights(layer)

    return layer


def test_all_logging(wandb_init_run):
    # TODO(jhr): does not work with --flake-finder
    net = ConvNet()
    wandb.watch(net, log="all", log_freq=1)
    for i in range(3):
        output = net(dummy_torch_tensor((32, 1, 28, 28)))
        grads = torch.ones(32, 10)
        output.backward(grads)
        wandb.log({"a": 2})
        assert len(wandb.run._backend.history[0]) == 20
        assert len(wandb.run._backend.history[0]["parameters/fc2.bias"]["bins"]) == 65
        assert len(wandb.run._backend.history[0]["gradients/fc2.bias"]["bins"]) == 65
    assert len(wandb.run._backend.history) == 3


def test_double_log(wandb_init_run):
    net = ConvNet()
    wandb.watch(net, log_graph=True)
    with pytest.raises(ValueError):
        wandb.watch(net, log_graph=True)


def test_embedding_dict_watch(wandb_init_run):
    model = EmbModelWrapper()
    wandb.watch(model, log_freq=1, idx=0)
    opt = torch.optim.Adam(params=model.parameters())
    inp = torch.randint(16, [8, 5])
    out = model(inp)
    out = (out["key"]["emb1"]).sum(-1)
    loss = F.mse_loss(out, inp.float())
    loss.backward()
    opt.step()
    wandb.log({"loss": loss})
    print(wandb.run._backend.history)
    assert len(wandb.run._backend.history[0]["gradients/emb.emb1.weight"]["bins"]) == 65


@pytest.mark.timeout(120)
def test_sequence_net(wandb_init_run):
    net = Sequence()
    graph = wandb.watch(net, log_graph=True)[0]
    output = net.forward(dummy_torch_tensor((97, 100)))
    output.backward(torch.zeros((97, 100)))
    graph = graph._to_graph_json()
    assert len(graph["nodes"]) == 3
    assert len(graph["nodes"][0]["parameters"]) == 4
    assert graph["nodes"][0]["class_name"] == "LSTMCell(1, 51)"
    assert graph["nodes"][0]["name"] == "lstm1"


@pytest.mark.skipif(
    sys.platform == "darwin", reason="TODO: [Errno 24] Too many open files?!?"
)
def test_multi_net(wandb_init_run):
    net1 = ConvNet()
    net2 = ConvNet()
    graphs = wandb.watch((net1, net2), log_graph=True)
    output1 = net1.forward(dummy_torch_tensor((64, 1, 28, 28)))
    output2 = net2.forward(dummy_torch_tensor((64, 1, 28, 28)))
    grads = torch.ones(64, 10)
    output1.backward(grads)
    output2.backward(grads)
    graph1 = graphs[0]._to_graph_json()
    graph2 = graphs[1]._to_graph_json()
    assert len(graph1["nodes"]) == 5
    assert len(graph2["nodes"]) == 5


def test_nested_shape():
    shape = wandb.wandb_torch.nested_shape([2, 4, 5])
    assert shape == [[], [], []]
    shape = wandb.wandb_torch.nested_shape(
        [dummy_torch_tensor((2, 3)), dummy_torch_tensor((4, 5))]
    )
    assert shape == [[2, 3], [4, 5]]
    # create recursive lists of tensors (t3 includes itself)
    t1 = dummy_torch_tensor((2, 3))
    t2 = dummy_torch_tensor((4, 5))
    t3 = [t1, t2]
    t3.append(t3)
    t3.append(t2)
    shape = wandb.wandb_torch.nested_shape([t1, t2, t3])
    assert shape == [[2, 3], [4, 5], [[2, 3], [4, 5], 0, [4, 5]]]


@pytest.mark.parametrize(
    "test_input,expected",
    [
        (torch.Tensor([1.0, 2.0, 3.0]), False),
        (torch.Tensor([0.0, 0.0, 0.0]), False),
        (torch.Tensor([1.0]), False),
        (torch.Tensor([]), True),
        (torch.Tensor([1.0, float("nan"), float("nan")]), False),
        (torch.Tensor([1.0, float("inf"), -float("inf")]), False),
        (torch.Tensor([1.0, float("nan"), float("inf")]), False),
        (torch.Tensor([float("nan"), float("nan"), float("nan")]), True),
        (torch.Tensor([float("inf"), float("inf"), -float("inf")]), True),
        (torch.Tensor([float("nan"), float("inf"), -float("inf")]), True),
    ],
)
def test_no_valid_values(test_input, expected, wandb_init_run):
    torch_history = wandb.wandb_torch.TorchHistory(wandb.run.history)

    assert torch_history._no_valid_values(test_input) is expected


@pytest.mark.parametrize(
    "test_input,expected",
    [
        (torch.Tensor([0.0, 1.0, 2.0]), torch.Tensor([0.0, 1.0, 2.0])),
        (torch.Tensor([1.0]), torch.Tensor([1.0])),
        (torch.Tensor([0.0, float("inf"), -float("inf")]), torch.Tensor([0.0])),
        (torch.Tensor([0.0, float("nan"), float("inf")]), torch.Tensor([0.0])),
    ],
)
def test_remove_invalid_entries(test_input, expected, wandb_init_run):
    torch_history = wandb.wandb_torch.TorchHistory(wandb.run.history)

    assert torch.equal(torch_history._remove_invalid_entries(test_input), expected)


def make_value_list(dtype: "torch.dtype") -> List[float]:
    if dtype == torch.float32:
        high = HIGH32
        eps = EPS32
        tiny = TINY32
    else:  # dtype == torch.float64
        high = HIGH64
        eps = EPS64
        tiny = TINY64

    return sorted(
        [
            # finite, reasonable values
            -2.0,
            -1.0,
            0.0,
            1.0,
            2.0,
            # large magnitude negatives
            -high,  # lowest
            -high * (1 - eps),  # very close to lowest
            -high * (1 - 65 * eps),  # low, but separated from lowest
            # low magnitude negatives
            -eps * tiny,  # lowest magnitude negative supported
            -2 * eps * tiny,  # almost lowest magnitude negative supported
            -66 * eps * tiny,  # separate from lowest
            # low mangnitude positives
            eps * tiny,  # lowest magnitude positive supported
            2 * eps * tiny,  # almost lowest magnitude positive supported
            66 * eps * tiny,  # separate from lowest
            # large magnitude positives
            high,  # highest
            high * (1 - eps),  # very close to highest
            high * (1 - 65 * eps),  # high, but separated from highest
        ]
    )


def make_bounds_list(dtype: torch.dtype) -> List[Tuple[float]]:
    values = make_value_list(dtype)

    return list(combinations_with_replacement(values, 2))


@pytest.mark.parametrize(
    "tmin,tmax,dtype",
    [
        (bounds[0], bounds[1], torch.float32)
        for bounds in make_bounds_list(torch.float32)
    ]
    + [
        (bounds[0], bounds[1], torch.float64)
        for bounds in make_bounds_list(torch.float64)
    ],
)
def test_widen_min_max(
    tmin: float, tmax: float, dtype: torch.dtype, wandb_init_run
) -> None:
    torch_history = wandb.wandb_torch.TorchHistory(wandb.run.history)

    if dtype == torch.float32:
        high = HIGH32
        eps = EPS32
        tiny = TINY32
    else:  # dtype == torch.float64
        high = HIGH64
        eps = EPS64
        tiny = TINY64

    tmin2, tmax2 = torch_history._widen_min_max(tmin, tmax, dtype)

    # The magnitude is still not extreme.
    assert -high * (1 + 32 * eps) <= tmin2 < tmax2 <= high * (1 + 32 * eps)

    # The interval width hasn't decreased.
    assert tmax2 - tmin2 >= tmax - tmin

    # Make sure the bounds don't change too much.
    middle = (tmin + tmax) / 2
    step_size = eps * max(abs(middle), tiny)
    assert tmin - tmin2 <= 100 * step_size
    assert tmax2 - tmax <= 100 * step_size

    # Make sure there's enough space between tmin2 and tmax2 for 64
    # distinct bins.
    assert torch.linspace(tmin2, tmax2, 65, dtype=dtype).unique().shape[0] == 65
