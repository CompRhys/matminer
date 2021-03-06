from __future__ import division, unicode_literals, print_function

import math
import itertools
from operator import itemgetter

import numpy as np
from scipy.stats import gaussian_kde
from pymatgen.analysis.diffraction.xrd import XRDCalculator
from pymatgen.analysis.local_env import ValenceIonicRadiusEvaluator
from pymatgen.core.periodic_table import Specie, Element
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

from matminer.featurizers.base import BaseFeaturizer


class RadialDistributionFunction(BaseFeaturizer):
    """
    Calculate the radial distribution function (RDF) of a crystal structure.

    Features:
        - Radial distribution function

    Args:
        cutoff: (float) distance up to which to calculate the RDF.
        bin_size: (float) size of each bin of the (discrete) RDF.
    """

    def __init__(self, cutoff=20.0, bin_size=0.1):
        self.cutoff = cutoff
        self.bin_size = bin_size

    def featurize(self, s):
        """
        Get RDF of the input structure.
        Args:
            s (Structure): Pymatgen Structure object.

        Returns:
            rdf, dist: (tuple of arrays) the first element is the
                    normalized RDF, whereas the second element is
                    the inner radius of the RDF bin.
        """
        if not s.is_ordered:
            raise ValueError("Disordered structure support not built yet")

        # Get the distances between all atoms
        neighbors_lst = s.get_all_neighbors(self.cutoff)
        all_distances = np.concatenate(
            tuple(map(lambda x: [itemgetter(1)(e) for e in x], neighbors_lst)))

        # Compute a histogram
        dist_hist, dist_bins = np.histogram(
            all_distances, bins=np.arange(
                0, self.cutoff + self.bin_size, self.bin_size), density=False)

        # Normalize counts
        shell_vol = 4.0 / 3.0 * math.pi * (np.power(
            dist_bins[1:], 3) - np.power(dist_bins[:-1], 3))
        number_density = s.num_sites / s.volume
        rdf = dist_hist / shell_vol / number_density
        return [{'distances': dist_bins[:-1], 'distribution': rdf}]

    def feature_labels(self):
        return ["radial distribution function"]

    def citations(self):
        return []

    def implementors(self):
        return ["Saurabh Bajaj"]


class PartialRadialDistributionFunction(BaseFeaturizer):
    """
    Compute the partial radial distribution function (PRDF) of an xtal structure

    The PRDF of a crystal structure is the radial distibution function broken
    down for each pair of atom types.  The PRDF was proposed as a structural
    descriptor by [Schutt *et al.*]
    (https://journals.aps.org/prb/abstract/10.1103/PhysRevB.89.205118)

    Args:
        cutoff: (float) distance up to which to calculate the RDF.
        bin_size: (float) size of each bin of the (discrete) RDF.
        include_elems: (list of string), list of elements that must be included in PRDF
        exclude_elems: (list of string), list of elmeents that should not be included in PRDF

    Features:
        Each feature corresponds to the density of number of bonds
           for a certain pair of elements at a certain range of
           distances. For example, "Al-Al PRDF r=1.00-1.50" corresponds
           to the density of Al-Al bonds between 1 and 1.5 distance units
           By default, this featurizer generates RDFs for each pair
           of elements in the training set."""

    def __init__(self, cutoff=20.0, bin_size=0.1, include_elems=(),
                 exclude_elems=()):
        self.cutoff = cutoff
        self.bin_size = bin_size
        self.elements_ = None
        self.include_elems = list(
            include_elems)  # Makes sure the element lists are ordered
        self.exclude_elems = list(exclude_elems)

    def fit(self, X, y=None):
        """Define the list of elements to be included in the PRDF. By default,
        the PRDF will include all of the elements in `X`

        Args:
            X: (numpy array nx1) structures used in the training set. Each entry
                must be Pymatgen Structure objects.
            y: *Not used*
            fit_kwargs: *not used*

        Returns:
            self
        """

        # Initialize list with included elements
        elements = set([Element(e) for e in self.include_elems])

        # Get all of elements that appaer
        for strc in X:
            elements.update([e.element if isinstance(e, Specie) else e for e in
                             strc.composition.keys()])

        # Remove the elements excluded by the user
        elements.difference_update([Element(e) for e in self.exclude_elems])

        # Store the elements
        self.elements_ = [e.symbol for e in sorted(elements)]

        return self

    def featurize(self, s):
        """
        Get PRDF of the input structure.
        Args:
            s: Pymatgen Structure object.

        Returns:
            prdf, dist: (tuple of arrays) the first element is a
                    dictionary where keys are tuples of element
                    names and values are PRDFs.
        """

        if not s.is_ordered:
            raise ValueError("Disordered structure support not built yet")
        if self.elements_ is None:
            raise Exception("You must run 'fit' first!")

        dist_bins, prdf = self.compute_prdf(
            s)  # Assemble the PRDF for each pair

        # Convert the PRDF into a feature array
        zeros = np.zeros_like(dist_bins)  # Zeros if elements don't appear
        output = []
        for key in itertools.combinations_with_replacement(self.elements_, 2):
            output.append(prdf.get(key, zeros))

        # Stack them together
        return np.hstack(output)

    def compute_prdf(self, s):
        """Compute the PRDF for a structure

        Args:
            s: (Structure), structure to be evaluated
        Returns:
            dist_bins - float, start of each of the bins
            prdf - dict, where the keys is a pair of elements (strings),
                and the value is the radial distribution function for
                those paris of elements
        """
        # Get the composition of the array
        composition = s.composition.fractional_composition.to_reduced_dict

        # Get the distances between all atoms
        neighbors_lst = s.get_all_neighbors(self.cutoff)

        # Sort neighbors by type
        distances_by_type = {}
        for p in itertools.product(composition.keys(), composition.keys()):
            distances_by_type[p] = []

        def get_symbol(site):
            if isinstance(site.specie, Element):
                return site.specie.symbol
            else:
                return site.specie.element.symbol

        # Each list is a list for each site
        for site, nlst in zip(s.sites, neighbors_lst):
            my_elem = get_symbol(site)

            for neighbor in nlst:
                rij = neighbor[1]
                n_elem = get_symbol(neighbor[0])
                # LW 3May17: Any better ideas than appending each element at a time?
                distances_by_type[(my_elem, n_elem)].append(rij)

        # Compute and normalize the prdfs
        prdf = {}
        dist_bins = self._make_bins()
        shell_volume = 4.0 / 3.0 * math.pi * (
                np.power(dist_bins[1:], 3) - np.power(dist_bins[:-1], 3))
        for key, distances in distances_by_type.items():
            # Compute histogram of distances
            dist_hist, dist_bins = np.histogram(distances, bins=dist_bins,
                                                density=False)
            # Normalize
            n_alpha = composition[key[0]] * s.num_sites
            rdf = dist_hist / shell_volume / n_alpha

            prdf[key] = rdf

        return dist_bins[:-1], prdf

    def _make_bins(self):
        """Generate the edges of the bins for the PRDF

        Returns:
            [list of float], edges of the bins
            """
        return np.arange(0, self.cutoff + self.bin_size, self.bin_size)

    def feature_labels(self):
        if self.elements_ is None:
            raise Exception("You must run 'fit' first!")
        bin_edges = self._make_bins()
        labels = []
        for e1, e2 in itertools.combinations_with_replacement(self.elements_,
                                                              2):
            for r_start, r_end in zip(bin_edges, bin_edges[1:]):
                labels.append("{}-{} PRDF r={:.2f}-{:.2f}".format(
                    e1, e2, r_start, r_end
                ))
        return labels

    def citations(self):
        return ["@article{Schutt2014,"
                "author = {Sch{\"{u}}tt, K. T. and Glawe, H. and Brockherde, F. "
                "and Sanna, A. and M{\"{u}}ller, K. R. and Gross, E. K. U.},"
                "doi = {10.1103/PhysRevB.89.205118},"
                "journal = {Physical Review B},"
                "month = {may},number = {20},pages = {205118},"
                "title = {{How to represent crystal structures for machine learning:"
                " Towards fast prediction of electronic properties}},"
                "url = {http://link.aps.org/doi/10.1103/PhysRevB.89.205118},"
                "volume = {89},""year = {2014}}"]

    def implementors(self):
        return ["Logan Ward", "Saurabh Bajaj"]


class ElectronicRadialDistributionFunction(BaseFeaturizer):
    """
    Calculate the inherent electronic radial distribution function (ReDF)

    The ReDF is defined according to Willighagen et al., Acta Cryst., 2005, B61,
    29-36.

    The ReDF is a structure-integral RDF (i.e., summed over
    all sites) in which the positions of neighboring sites
    are weighted by electrostatic interactions inferred
    from atomic partial charges. Atomic charges are obtained
    from the ValenceIonicRadiusEvaluator class.

    Args:
        cutoff: (float) distance up to which the ReDF is to be
                calculated (default: longest diagaonal in
                primitive cell).
        dr: (float) width of bins ("x"-axis) of ReDF (default: 0.05 A).
    """

    def __init__(self, cutoff=None, dr=0.05):
        self.cutoff = cutoff
        self.dr = dr

    def featurize(self, s):
        """
        Get ReDF of input structure.

        Args:
            s: input Structure object.

        Returns: (dict) a copy of the electronic radial distribution
                functions (ReDF) as a dictionary. The distance list
                ("x"-axis values of ReDF) can be accessed via key
                'distances'; the ReDF itself is accessible via key
                'redf'.
        """
        if self.dr <= 0:
            raise ValueError("width of bins for ReDF must be >0")

        # Make structure primitive.
        struct = SpacegroupAnalyzer(s).find_primitive() or s

        # Add oxidation states.
        struct = ValenceIonicRadiusEvaluator(struct).structure

        if self.cutoff is None:
            # Set cutoff to longest diagonal.
            a = struct.lattice.matrix[0]
            b = struct.lattice.matrix[1]
            c = struct.lattice.matrix[2]
            self.cutoff = max(
                [np.linalg.norm(a + b + c), np.linalg.norm(-a + b + c),
                 np.linalg.norm(a - b + c), np.linalg.norm(a + b - c)])

        nbins = int(self.cutoff / self.dr) + 1
        redf_dict = {"distances": np.array(
            [(i + 0.5) * self.dr for i in range(nbins)]),
            "distribution": np.zeros(nbins, dtype=np.float)}

        for site in struct.sites:
            this_charge = float(site.specie.oxi_state)
            neighbors = struct.get_neighbors(site, self.cutoff)
            for nnsite, dist, *_ in neighbors:
                neigh_charge = float(nnsite.specie.oxi_state)
                bin_index = int(dist / self.dr)
                redf_dict["distribution"][bin_index] \
                    += (this_charge * neigh_charge) / (struct.num_sites * dist)

        return [redf_dict]

    def feature_labels(self):
        return ["electronic radial distribution function"]

    def citations(self):
        return ["@article{title={Method for the computational comparison"
                " of crystal structures}, volume={B61}, pages={29-36},"
                " DOI={10.1107/S0108768104028344},"
                " journal={Acta Crystallographica Section B},"
                " author={Willighagen, E. L. and Wehrens, R. and Verwer,"
                " P. and de Gelder R. and Buydens, L. M. C.}, year={2005}}"]

    def implementors(self):
        return ["Nils E. R. Zimmermann"]


class XRDPowderPattern(BaseFeaturizer):
    """
    1D array representing powder diffraction of a structure as calculated by
    pymatgen. The powder is smeared / normalized according to gaussian_kde.

    NOTE comprhys: placed in distribution as the rdf and ssf are related by
    a fourier transform.
    """

    def __init__(self, two_theta_range=(0, 127), bw_method=0.05,
                 pattern_length=None, **kwargs):
        """
        Initialize the featurizer.

        Args:
            two_theta_range ([float of length 2]): Tuple for range of
                two_thetas to calculate in degrees. Defaults to (0, 90). Set to
                None if you want all diffracted beams within the limiting
                sphere of radius 2 / wavelength.
            bw_method (float): how much to smear the XRD pattern
            pattern_length (float): length of final array; defaults to one value
             per degree (i.e. two_theta_range + 1)
            **kwargs: any other arguments to pass into pymatgen's XRDCalculator,
                such as the type of radiation.
        """
        self.two_theta_range = two_theta_range
        self.bw_method = bw_method
        self.pattern_length = pattern_length or two_theta_range[1] - \
                              two_theta_range[0] + 1
        self.xrd_calc = XRDCalculator(**kwargs)

    def featurize(self, strc):
        pattern = self.xrd_calc.get_pattern(
            strc, two_theta_range=self.two_theta_range)
        x, y = pattern.x, pattern.y
        hist = []
        for x1, y1 in zip(x, y):
            num = int(y1)
            hist += [x1] * num

        kernel = gaussian_kde(hist, bw_method=self.bw_method)
        x = np.linspace(self.two_theta_range[0], self.two_theta_range[1],
                        self.pattern_length)
        y = kernel(x)

        return y

    def feature_labels(self):
        return ['xrd_{}'.format(x) for x in range(self.pattern_length)]

    def citations(self):
        return ["@article{Ong2013, author = {Ong, Shyue Ping and Richards, "
                "William Davidson and Jain, Anubhav and Hautier, "
                "Geoffroy and Kocher, Michael and Cholia, Shreyas and Gunter, "
                "Dan and Chevrier, Vincent L. and Persson, "
                "Kristin A. and Ceder, Gerbrand}, "
                "doi = {10.1016/j.commatsci.2012.10.028}, issn = {09270256}, "
                "journal = {Computational Materials Science}, month = {feb}, "
                "pages = {314--319}, "
                "publisher = {Elsevier B.V.}, title = {{Python Materials "
                "Genomics (pymatgen): A robust, open-source python "
                "library for materials analysis}}, url = "
                "{http://linkinghub.elsevier.com/retrieve/pii/S0927025612006295}, "
                "volume = {68}, year = {2013} } "]

    def implementors(self):
        return ['Anubhav Jain', 'Matthew Horton']
