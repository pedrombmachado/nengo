import warnings

import numpy as np

import nengo
from nengo.exceptions import ObsoleteError, ValidationError
from nengo.networks.product import Product


def circconv(a, b, invert_a=False, invert_b=False, axis=-1):
    """A reference Numpy implementation of circular convolution."""
    A = np.fft.fft(a, axis=axis)
    B = np.fft.fft(b, axis=axis)
    if invert_a:
        A = A.conj()
    if invert_b:
        B = B.conj()
    return np.fft.ifft(A * B, axis=axis).real


def transform_in(dims, align, invert):
    """Create a transform to map the input into the Fourier domain.

    See CircularConvolution docstring for more details.

    Parameters
    ----------
    dims : int
        Input dimensions.
    align : 'A' or 'B'
        How to align the real and imaginary components; the alignment
        depends on whether we're doing transformA or transformB.
    invert : bool
        Whether to reverse the order of elements.
    """
    if align not in ("A", "B"):
        raise ValidationError("'align' must be either 'A' or 'B'", "align")

    dims2 = 4 * (dims // 2 + 1)
    tr = np.zeros((dims2, dims))
    dft = dft_half(dims)

    for i in range(dims2):
        row = dft[i // 4] if not invert else dft[i // 4].conj()
        if align == "A":
            tr[i] = row.real if i % 2 == 0 else row.imag
        else:  # align == 'B'
            tr[i] = row.real if i % 4 == 0 or i % 4 == 3 else row.imag

    remove_imag_rows(tr)
    return tr.reshape((-1, dims))


def transform_out(dims):
    dims2 = dims // 2 + 1
    tr = np.zeros((dims2, 4, dims))
    idft = dft_half(dims).conj()

    for i in range(dims2):
        row = idft[i] if i == 0 or 2 * i == dims else 2 * idft[i]
        tr[i, 0] = row.real
        tr[i, 1] = -row.real
        tr[i, 2] = -row.imag
        tr[i, 3] = -row.imag

    tr = tr.reshape(4 * dims2, dims)
    remove_imag_rows(tr)
    # IDFT has a 1/D scaling factor
    tr /= dims

    return tr.T


def remove_imag_rows(tr):
    """Throw away imaginary rows we do not need (they are zero)."""
    i = np.arange(tr.shape[0])
    if tr.shape[1] % 2 == 0:
        tr = tr[(i == 0) | (i > 3) & (i < len(i) - 3)]
    else:
        tr = tr[(i == 0) | (i > 3)]


def dft_half(n):
    x = np.arange(n)
    w = np.arange(n // 2 + 1)
    return np.exp((-2.0j * np.pi / n) * (w[:, None] * x[None, :]))


class CircularConvolution(nengo.Network):
    r"""Compute the circular convolution of two vectors.

    The circular convolution :math:`c` of vectors :math:`a` and :math:`b`
    is given by

    .. math:: c[i] = \sum_j a[j] b[i - j]

    where negative indices on :math:`b` wrap around to the end of the vector.

    This computation can also be done in the Fourier domain,

    .. math:: c = DFT^{-1} ( DFT(a) DFT(b) )

    where :math:`DFT` is the Discrete Fourier Transform operator, and
    :math:`DFT^{-1}` is its inverse. This network uses this method.

    Parameters
    ----------
    n_neurons : int
        Number of neurons to use in each product computation
    dimensions : int
        The number of dimensions of the input and output vectors.

    invert_a, invert_b : bool, optional
        Whether to reverse the order of elements in either
        the first input (``invert_a``) or the second input (``invert_b``).
        Flipping the second input will make the network perform circular
        correlation instead of circular convolution.
    input_magnitude : float, optional
        The expected magnitude of the vectors to be convolved.
        This value is used to determine the radius of the ensembles
        computing the element-wise product.
    **kwargs
        Keyword arguments passed through to ``nengo.Network``
        like 'label' and 'seed'.

    Attributes
    ----------
    input_a : Node
        The first vector to be convolved.
    input_b : Node
        The second vector to be convolved.
    product : Network
        Network created with `.Product` to do the element-wise product
        of the :math:`DFT` components.
    output : Node
        The resulting convolved vector.

    Examples
    --------

    A basic example computing the circular convolution of two 10-dimensional
    vectors represented by ensemble arrays::

        A = EnsembleArray(50, n_ensembles=10)
        B = EnsembleArray(50, n_ensembles=10)
        C = EnsembleArray(50, n_ensembles=10)
        cconv = nengo.networks.CircularConvolution(50, dimensions=10)
        nengo.Connection(A.output, cconv.input_a)
        nengo.Connection(B.output, cconv.input_b)
        nengo.Connection(cconv.output, C.input)

    Notes
    -----

    The network maps the input vectors :math:`a` and :math:`b` of length N into
    the Fourier domain and aligns them for complex multiplication.
    Letting :math:`F = DFT(a)` and :math:`G = DFT(b)`, this is given by::

        [ F[i].real ]     [ G[i].real ]     [ w[i] ]
        [ F[i].imag ]  *  [ G[i].imag ]  =  [ x[i] ]
        [ F[i].real ]     [ G[i].imag ]     [ y[i] ]
        [ F[i].imag ]     [ G[i].real ]     [ z[i] ]

    where :math:`i` only ranges over the lower half of the spectrum, since
    the upper half of the spectrum is the flipped complex conjugate of
    the lower half, and therefore redundant. The input transforms are
    used to perform the DFT on the inputs and align them correctly for
    complex multiplication.

    The complex product :math:`H = F * G` is then

    .. math:: H[i] = (w[i] - x[i]) + (y[i] + z[i]) I

    where :math:`I = \sqrt{-1}`. We can perform this addition along with the
    inverse DFT :math:`c = DFT^{-1}(H)` in a single output transform, finding
    only the real part of :math:`c` since the imaginary part
    is analytically zero.
    """

    def __init__(
        self,
        n_neurons,
        dimensions,
        invert_a=False,
        invert_b=False,
        input_magnitude=1.0,
        # fmt: off
        **kwargs
        # fmt: on
    ):
        if "net" in kwargs:
            raise ObsoleteError("The 'net' argument is no longer supported.")
        kwargs.setdefault("label", "Circular convolution")
        super().__init__(**kwargs)

        tr_a = transform_in(dimensions, "A", invert_a)
        tr_b = transform_in(dimensions, "B", invert_b)
        tr_out = transform_out(dimensions)

        with self:
            self.input_a = nengo.Node(size_in=dimensions, label="input_a")
            self.input_b = nengo.Node(size_in=dimensions, label="input_b")
            self.product = Product(
                n_neurons, tr_out.shape[1], input_magnitude=input_magnitude * 2
            )
            self.output = nengo.Node(size_in=dimensions, label="output")

            nengo.Connection(
                self.input_a, self.product.input_a, transform=tr_a, synapse=None
            )
            nengo.Connection(
                self.input_b, self.product.input_b, transform=tr_b, synapse=None
            )
            nengo.Connection(
                self.product.output, self.output, transform=tr_out, synapse=None
            )

    @property
    def A(self):
        warnings.warn(DeprecationWarning("Use 'input_a' instead of 'A'"))
        return self.input_a

    @property
    def B(self):
        warnings.warn(DeprecationWarning("Use 'input_b' instead of 'B'."))
        return self.input_b
