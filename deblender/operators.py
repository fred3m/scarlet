from __future__ import print_function, division
import logging

import numpy as np
import scipy.sparse

logger = logging.getLogger("deblender.operators")

class Translations:
    def __init__(self, peaks, shape, B=None, P=None, differential=0.1, max_shift=2, threshold=1e-8,
                 fit_positions=True, wait=0, skip=10):
        """Initialize the class
        
        The class is initialized with its shape and the initial differential operators
        """
        cx, cy = int(shape[1]/2), int(shape[0]/2)
        self.peaks = [[cx-px, cy-py] for px,py in peaks]
        self.init_peaks = self.peaks.copy()
        self.shape = shape
        self.size = shape[0]*shape[1]
        self.B = B
        self.P = P
        self.differential = differential
        self.max_shift =  max_shift
        self.threshold = threshold
        self.fit_positions = fit_positions
        self.wait = wait
        self.iteration = 0
        self.skip = skip

        # tx, ty have an entry for each integer shift of a peak
        self.tx = {}
        self.ty = {}
        self.Tx = [None]*len(peaks)
        self.Ty = [None]*len(peaks)
        self.Gamma = None

        # Create the initial translation matrices
        for k, (px, py) in enumerate(self.peaks):
            self.get_translation_ops(k, update=True)
        # Update the initial Gamma matrices
        self.translate_psfs()

    def get_int_shift(self, dx, dy, threshold=None):
        """Calculate the integer shifts, which sets the diagonal in the matrices
        """
        # Use the default threshold unless it's specified
        if threshold is not None:
            self.threshold = threshold
        int_dx, int_dy = int(dx), int(dy)
        
        # If the fractional shifts are less than the threshold,
        # round to the nearest integer shift
        if np.abs(dx-int_dx) < self.threshold:
            int_dx = int(np.round(dx))
            dx = int_dx
        if np.abs(dy-int_dy) < self.threshold:
            int_dy = int(np.round(dy))
            dy = int_dy
        return int_dx, int_dy, dx, dy

    def build_Tx(self, int_dx):
        """Construct Tx and its components
        """
        height, width = self.shape
        tx = scipy.sparse.diags([1],[int_dx], shape=(width, width), dtype=np.float64)
        tx_plus = scipy.sparse.diags([-1,1],[int_dx, int_dx+1],
                                     shape=(width, width), dtype=np.float64)
        tx_minus = scipy.sparse.diags([1,-1],[int_dx, int_dx-1],
                                      shape=(width, width), dtype=np.float64)
        tx = scipy.sparse.block_diag([tx]*height)
        tx_plus = scipy.sparse.block_diag([tx_plus]*height)
        tx_minus = scipy.sparse.block_diag([tx_minus]*height)
        self.tx[int_dx] = (tx, tx_plus, tx_minus)

    def build_Ty(self, int_dy):
        """Construct Ty and its components
        """
        width = self.shape[1]
        ty = scipy.sparse.diags([1], [int_dy*width], shape=(self.size, self.size), dtype=np.float64)
        ty_plus = scipy.sparse.diags([-1, 1], [int_dy*width, (int_dy+1)*width],
                                     shape=(self.size, self.size), dtype=np.float64)
        ty_minus = scipy.sparse.diags([1, -1], [int_dy*width, (int_dy-1)*width],
                                      shape=(self.size, self.size), dtype=np.float64)
        self.ty[int_dy] = (ty, ty_plus, ty_minus)

    def get_translation_ops(self, k, ddx=0, ddy=0, update=False):
        """Get the operators needed to shift peak k
        """
        dx, dy = self.peaks[k]
        dx += ddx
        dy += ddy
        int_dx, int_dy, dx, dy = self.get_int_shift(dx, dy)
        if update:
            self.peaks[k] = [dx, dy]
        # Build Tx and Ty (if necessary)
        if int_dx not in self.tx.keys():
            self.build_Tx(int_dx)
        if int_dy not in self.ty.keys():
            self.build_Ty(int_dy)
        tx, tx_plus, tx_minus = self.tx[int_dx]
        ty, ty_plus, ty_minus = self.ty[int_dy]
        # Create Tx
        if dx<0:
            dtx = tx_minus
        else:
            dtx = tx_plus
        Tx = tx + (dx-int_dx)*dtx
        # Create Ty
        if dy<0:
            dty = ty_minus
        else:
            dty = ty_plus
        Ty = ty + (dy-int_dy)*dty
        # Optionally store the operators for the peak
        if update:
            self.Tx[k] = Tx
            self.Ty[k] = Ty
        return Tx, Ty

    def translate_psfs(self):
        """Translate the PSFs using Tx and Ty
        """
        Gamma = []
        for k in range(len(self.peaks)):
            if self.P is None:
                gamma = [self.Ty[k].dot(self.Tx[k])]*self.B
            else:
                gamma = []
                for b in range(B):
                    g = self.Ty[pk].dot(self.P[b].dot(self.Tx[k]))
                    gamma.append(g)
            Gamma.append(gamma)
        self.Gamma = Gamma

    def update_positions(self, data, models, A, S, P=None, W=None):
        """Update the x and y position of peak k
        """
        # Wait for the specified number of iterations
        self.iteration += 1
        if self.iteration > self.wait and self.iteration % self.skip!=0:
            return self.Tx, self.Ty
        from .nmf import get_peak_model

        dxy = self.differential
        dipoles = []
        for k, (dx,dy) in enumerate(self.peaks):
            model = models[k]
            Tx, Ty = self.get_translation_ops(k, dxy, dxy)
            # Get the difference image in x by adjusting only the x
            # component by the differential amount dxy
            diff_img = get_peak_model(A[:,k], S[k], Tx, self.Ty[k], P=P)
            dipole = (model-diff_img)/dxy
            dipoles.append(dipole)
            # Do the same for the y difference image
            diff_img = get_peak_model(A[:,k], S[k], self.Tx[k], Ty, P=P)
            dipole = (model-diff_img)/dxy
            dipoles.append(dipole)
        model = np.sum(models, axis=0)
        A = np.vstack([-dp.flatten() for dp in dipoles]).T
        y = (data-model).flatten()
        results = np.linalg.lstsq(A, y)[0]

        for k, (px, py) in enumerate(self.peaks):
            ddx = results[2*k]
            ddy = results[2*k+1]
            ipx, ipy = self.init_peaks[k]
            # Check that the total shift doesn't exceed the maximum
            if np.abs(px+ddx-ipx) > self.max_shift:
                logger.warn("Attempted to shift peak {0} x position greater than max_shift".format(k))
                self.peaks[k][0] = self.init_peaks[k][0]
                ddx = 0
            if np.abs(py+ddy-ipy) > self.max_shift:
                logger.warn("Attempted to shift peak {0} y position greater than max_shift".format(k))
                self.peaks[k][1] = self.init_peaks[k][1]
                ddy = 0
                logger.info("peak {0}: py:{1}, ipy:{2}, ddy:{3}".format(k,py, ipy, ddy))
            # Update the peak positions and build the new Tx, Ty
            self.get_translation_ops(k, ddx, ddy, update=True)
        self.translate_psfs()
        return self.Tx, self.Ty

def getZeroOp(shape):
    size = shape[0]*shape[1]
    # matrix with ones on diagonal shifted by k, here out of matrix: all zeros
    return scipy.sparse.eye(size,k=size)

def getIdentityOp(shape):
    size = shape[0]*shape[1]
    return scipy.sparse.identity(size)

def getSymmetryOp(shape):
    """Create a linear operator to symmetrize an image

    Given the ``shape`` of an image, create a linear operator that
    acts on the flattened image to return its symmetric version.
    """
    size = shape[0]*shape[1]
    idx = np.arange(shape[0]*shape[1])
    sidx = idx[::-1]
    symmetryOp = scipy.sparse.identity(size)
    symmetryOp -= scipy.sparse.coo_matrix((np.ones(size),(idx, sidx)), shape=(size,size))
    return symmetryOp

def getOffsets(width, coords=None):
    """Get the offset and slices for a sparse band diagonal array

    For an operator that interacts with its neighbors we want a band diagonal matrix,
    where each row describes the 8 pixels that are neighbors for the reference pixel
    (the diagonal). Regardless of the operator, these 8 bands are always the same,
    so we make a utility function that returns the offsets (passed to scipy.sparse.diags).

    See `diagonalizeArray` for more on the slices and format of the array used to create
    NxN operators that act on a data vector.
    """
    # Use the neighboring pixels by default
    if coords is None:
        coords = [(-1,-1), (-1,0), (-1, 1), (0,-1), (0,1), (1, -1), (1,0), (1,1)]
    offsets = [width*y+x for y,x in coords]
    slices = [slice(None, s) if s<0 else slice(s, None) for s in offsets]
    slicesInv = [slice(-s, None) if s<0 else slice(None, -s) for s in offsets]
    return offsets, slices, slicesInv

def diagonalizeArray(arr, shape=None, dtype=np.float64):
    """Convert an array to a matrix that compares each pixel to its neighbors

    Given an array with length N, create an 8xN array, where each row will be a
    diagonal in a diagonalized array. Each column in this matrix is a row in the larger
    NxN matrix used for an operator, except that this 2D array only contains the values
    used to create the bands in the band diagonal matrix.

    Because the off-diagonal bands have less than N elements, ``getOffsets`` is used to
    create a mask that will set the elements of the array that are outside of the matrix to zero.

    ``arr`` is the vector to diagonalize, for example the distance from each pixel to the peak,
    or the angle of the vector to the peak.

    ``shape`` is the shape of the original image.
    """
    if shape is None:
        height, width = arr.shape
        data = arr.flatten()
    elif len(arr.shape)==1:
        height, width = shape
        data = np.copy(arr)
    else:
        raise ValueError("Expected either a 2D array or a 1D array and a shape")
    size = width * height

    # We hard code 8 rows, since each row corresponds to a neighbor
    # of each pixel.
    diagonals = np.zeros((8, size), dtype=dtype)
    mask = np.ones((8, size), dtype=bool)
    offsets, slices, slicesInv = getOffsets(width)
    for n, s in enumerate(slices):
        diagonals[n][slicesInv[n]] = data[s]
        mask[n][slicesInv[n]] = 0

    # Create a mask to hide false neighbors for pixels on the edge
    # (for example, a pixel on the left edge should not be connected to the
    # pixel to its immediate left in the flattened vector, since that pixel
    # is actual the far right pixel on the row above it).
    mask[0][np.arange(1,height)*width] = 1
    mask[2][np.arange(height)*width-1] = 1
    mask[3][np.arange(1,height)*width] = 1
    mask[4][np.arange(1,height)*width-1] = 1
    mask[5][np.arange(height)*width] = 1
    mask[7][np.arange(1,height-1)*width-1] = 1

    return diagonals, mask

def diagonalsToSparse(diagonals, shape, dtype=np.float64):
    """Convert a diagonalized array into a sparse diagonal matrix

    ``diagonalizeArray`` creates an 8xN array representing the bands that describe the
    interactions of a pixel with its neighbors. This function takes that 8xN array and converts
    it into a sparse diagonal matrix.

    See `diagonalizeArray` for the details of the 8xN array.
    """
    height, width = shape
    offsets, slices, slicesInv = getOffsets(width)
    diags = [diag[slicesInv[n]] for n, diag in enumerate(diagonals)]
    diagonalArr = scipy.sparse.diags(diags, offsets, dtype=dtype)
    return diagonalArr

def getRadialMonotonicOp(shape, useNearest=True, minGradient=1):
    """Create an operator to constrain radial monotonicity

    This version of the radial monotonicity operator selects all of the pixels closer to the peak
    for each pixel and weights their flux based on their alignment with a vector from the pixel
    to the peak. In order to quickly create this using sparse matrices, its construction is a bit opaque.
    """
    # Center on the center pixel
    px = int(shape[1]/2)
    py = int(shape[0]/2)
    # Calculate the distance between each pixel and the peak
    size = shape[0]*shape[1]
    x = np.arange(shape[1])
    y = np.arange(shape[0])
    X,Y = np.meshgrid(x,y)
    X = X - px
    Y = Y - py
    distance = np.sqrt(X**2+Y**2)

    # Find each pixels neighbors further from the peak and mark them as invalid
    # (to be removed later)
    distArr, mask = diagonalizeArray(distance, dtype=np.float64)
    relativeDist = (distance.flatten()[:,None]-distArr.T).T
    invalidPix = relativeDist<=0

    # Calculate the angle between each pixel and the x axis, relative to the peak position
    # (also avoid dividing by zero and set the tan(infinity) pixel values to pi/2 manually)
    inf = X==0
    tX = X.copy()
    tX[inf] = 1
    angles = np.arctan2(-Y,-tX)
    angles[inf&(Y!=0)] = 0.5*np.pi*np.sign(angles[inf&(Y!=0)])

    # Calcualte the angle between each pixel and it's neighbors
    xArr, m = diagonalizeArray(X)
    yArr, m = diagonalizeArray(Y)
    dx = (xArr.T-X.flatten()[:, None]).T
    dy = (yArr.T-Y.flatten()[:, None]).T
    # Avoid dividing by zero and set the tan(infinity) pixel values to pi/2 manually
    inf = dx==0
    dx[inf] = 1
    relativeAngles = np.arctan2(dy,dx)
    relativeAngles[inf&(dy!=0)] = 0.5*np.pi*np.sign(relativeAngles[inf&(dy!=0)])

    # Find the difference between each pixels angle with the peak
    # and the relative angles to its neighbors, and take the
    # cos to find its neighbors weight
    dAngles = (angles.flatten()[:, None]-relativeAngles.T).T
    cosWeight = np.cos(dAngles)
    # Mask edge pixels, array elements outside the operator (for offdiagonal bands with < N elements),
    # and neighbors further from the peak than the reference pixel
    cosWeight[invalidPix] = 0
    cosWeight[mask] = 0

    if useNearest:
        # Only use a single pixel most in line with peak
        cosNorm = np.zeros_like(cosWeight)
        columnIndices =  np.arange(cosWeight.shape[1])
        maxIndices = np.argmax(cosWeight, axis=0)
        indices = maxIndices*cosNorm.shape[1]+columnIndices
        indices = np.unravel_index(indices, cosNorm.shape)
        cosNorm[indices] = minGradient
        # Remove the reference for the peak pixel
        cosNorm[:,px+py*shape[1]] = 0
    else:
        # Normalize the cos weights for each pixel
        normalize = np.sum(cosWeight, axis=0)
        normalize[normalize==0] = 1
        cosNorm = (cosWeight.T/normalize[:,None]).T
        cosNorm[mask] = 0
    cosArr = diagonalsToSparse(cosNorm, shape)

    # The identity with the peak pixel removed represents the reference pixels
    diagonal = np.ones(size)
    diagonal[px+py*shape[1]] = -1
    monotonic = cosArr-scipy.sparse.diags(diagonal, offsets=0)

    return monotonic.tocoo()

def getPSFOp(psf, imgShape):
    """Create an operator to convolve intensities with the PSF

    Given a psf image ``psf`` and the shape of the blended image ``imgShape``,
    make a banded matrix out of all non-zero pixels in ``psfImg`` that acts as
    the PSF operator.
    """
    height, width = imgShape
    size = width * height

    # Calculate the coordinates of the pixels in the psf image above the threshold
    indices = np.where(psf != 0)
    indices = np.dstack(indices)[0]
    # assume all PSF images have odd dimensions and are centered!
    cy, cx = psf.shape[0]//2, psf.shape[1]//2
    coords = indices-np.array([cy,cx])

    # Create the PSF Operator
    offsets, slices, slicesInv = getOffsets(width, coords)
    psfDiags = [psf[y,x] for y,x in indices]
    psfOp = scipy.sparse.diags(psfDiags, offsets, shape=(size, size), dtype=np.float64)
    psfOp = psfOp.tolil()

    # Remove entries for pixels on the left or right edges
    cxRange = np.unique([cx for cy,cx in coords])
    for h in range(height):
        for y,x in coords:
            # Left edge
            if x<0 and width*(h+y)+x>=0 and h+y<=height:
                psfOp[width*h, width*(h+y)+x] = 0

                # Pixels closer to the left edge
                # than the radius of the psf
                for x_ in cxRange[cxRange<0]:
                    if (x<x_ and
                        width*h-x_>=0 and
                        width*(h+y)+x-x_>=0 and
                        h+y<=height
                    ):
                        psfOp[width*h-x_, width*(h+y)+x-x_] = 0

            # Right edge
            if x>0 and width*(h+1)-1>=0 and width*(h+y+1)+x-1>=0 and h+y<=height and width*(h+1+y)+x-1<size:
                psfOp[width*(h+1)-1, width*(h+y+1)+x-1] = 0

                for x_ in cxRange[cxRange>0]:
                    # Near right edge
                    if (x>x_ and
                        width*(h+1)-x_-1>=0 and
                        width*(h+y+1)+x-x_-1>=0 and
                        h+y<=height and
                        width*(h+1+y)+x-x_-1<size
                    ):
                        psfOp[width*(h+1)-x_-1, width*(h+y+1)+x-x_-1] = 0

    # Return the transpose, which correctly convolves the data with the PSF
    return psfOp.T.tocoo()

# ring-shaped masks around the peak
def getRingMask(im_shape, peak, outer, inner=0, flatten=False):
    height,width = im_shape
    x,y = np.meshgrid(np.arange(width), np.arange(height))
    r = np.sqrt((x-peak[1])**2 + (y-peak[0])**2)
    mask = (r < inner) | (r >= outer)
    if flatten:
        return mask.flatten()
    return mask

# odd-integer downsampling
def downsample(S, oversampling, mask=None):
    assert isinstance(oversampling, (int, long))
    if oversampling <= 1:
        return S
    else:
        height,width = S.shape
        height /= oversampling
        width /= oversampling
        Sd = np.zeros((height, width), dtype=S.dtype)
        if mask is None:
            S_ = S
        else:
            S_ = S*(~mask)
        # TODO: can we avoid the double loop?
        for h in range(height):
            for w in range(width):
                Sd[h,w] = S_[h*oversampling:(h+1)*oversampling, w*oversampling:(w+1)*oversampling].sum() / oversampling**2
        return Sd

# construct spin-wave decomposition operator for given list of spin numbers m
# radial behavior can be specified as appropriate
def getSpinOp(ms, shape, thickness=4, peak=None, oversampling=21, radial_fct=lambda r:1./np.maximum(1,r)):
    """ Spin decomposition operator.

    The operator maps onto a basis function of R(r) exp(i m phi), where phi is
    the polar angle wrt to the peak (or, if None, the center of the image).

    The decomposition is performed in a set of concentric rings of fixed thickness.

    ms is a list of integers that indicate the requested spin numbers.
    thickness is the radial separation between the inner and outer ring radius.
    peak is (an optional) offset of the object from the image center.
    oversampling determine the higher-resolution grid for the in-pixel
    integration; it must be odd.
    radial_fct is the radial part of the spin basis function.

    """
    assert oversampling % 2 == 1
    assert hasattr(ms, '__iter__')

    height,width = shape
    assert height % 2 == 0 and width % 2 == 0

    if peak is None:
        peak = [height/2, width/2]
    x,y = np.meshgrid(np.arange(width*oversampling), np.arange(height*oversampling))
    x = x * 1./oversampling - peak[1]
    y = y * 1./oversampling - peak[0]
    # proper treatment of over & downsampling: center pixel location
    if oversampling > 1:
        x -= 0.5 - 0.5/oversampling
        y -= 0.5 - 0.5/oversampling
    # convert to polar
    r = np.sqrt(x**2 + y**2)
    phi = np.arctan2(y,x)

    # define series of radial ring-shaped masks (oversampled as well)
    r_limit = (np.min([peak[0], height-peak[0], peak[1], width-peak[1]]) - 1)*oversampling
    base = thickness*oversampling
    lims = [(base*(i+1), base*i) for i in range(r_limit/base)]
    mask_peak = ((peak[0]+0.5)*oversampling - 0.5, (peak[1]+0.5)*oversampling - 0.5)
    masks = [getRingMask(r.shape, mask_peak, outer, inner) for outer, inner in lims]

    Ss = []
    for i in range(len(ms)):
        m = ms[i]
        spin = radial_fct(r) * np.exp(1j*m*phi)
        for j in range(len(masks)):
            mask = masks[j]
            S = downsample(spin, oversampling, mask=mask).flatten()
            Ss.append(S)

    # TODO: make Ss sparse and split real and imaginary part
    return np.array(Ss)
