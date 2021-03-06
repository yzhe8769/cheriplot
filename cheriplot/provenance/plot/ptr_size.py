#-
# Copyright (c) 2016-2017 Alfredo Mazzinghi
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# @BERI_LICENSE_HEADER_START@
#
# Licensed to BERI Open Systems C.I.C. (BERI) under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  BERI licenses this
# file to you under the BERI Hardware-Software License, Version 1.0 (the
# "License"); you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at:
#
#   http://www.beri-open-systems.org/legal/license-1-0.txt
#
# Unless required by applicable law or agreed to in writing, Work distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations under the License.
#
# @BERI_LICENSE_HEADER_END@
#

import numpy as np
import pandas as pd
import logging
import os

import matplotlib.colors as colors
import matplotlib.cm as colormap

from scipy import stats
from matplotlib import pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
from matplotlib.transforms import Bbox
from matplotlib.font_manager import FontProperties
from graph_tool.all import GraphView

from cheriplot.core import (
    ProgressTimer, ProgressPrinter, ExternalLegendTopPlotBuilder,
    BasePlotBuilder, PatchBuilder, LabelManager, AutoText, TaskDriver,
    Option, Argument)
from cheriplot.provenance.visit import (
    FilterNullVertices, FilterKernelVertices, FilterCfromptr, MergeCfromptr)
from cheriplot.provenance.parser import CheriMipsModelParser
from cheriplot.provenance.plot import VMMapPlotDriver

logger = logging.getLogger(__name__)

class CapSizeHistogram:
    """
    Base class for the histogram data structure building strategy
    """

    def __init__(self, provenance_graph, vmmap):
        self.graph = provenance_graph
        """The provenance graph"""

        self.vmmap = vmmap
        """VMMap object representing the process memory map."""

        self.n_bins = [0, 10, 20, 21, 22, 23, 64]
        """Bin edges for capability size, notice that the size is log2."""

        self.norm_histogram = pd.DataFrame(columns=self.n_bins[1:])
        """List of normalized histograms for each vmmap entry."""

        self.abs_histogram = pd.DataFrame(columns=self.n_bins[1:])
        """List of histograms for each vmmap entry."""

        self._build_histogram()

    def _build_histogram(self):
        hist_data = self._build_histogram_input()
        for vm_entry, data in zip(self.vmmap, hist_data):
            if len(data) == 0:
                continue
            # the bin size is logarithmic
            data = np.log2(data)
            h, b = np.histogram(data, bins=self.n_bins)
            # append histogram to the dataframes
            self.abs_histogram.loc[vm_entry] = h
            self.norm_histogram.loc[vm_entry] = h / np.sum(h)

    def _build_histogram_input(self):
        """
        Build the input data structure containing a list of data to use
        for the histogram for each entry in the vmmap entry.
        """
        return None


class CapSizeDerefHistogram(CapSizeHistogram):
    """
    Histogram that takes into account capabilities at dereference time.
    The address space is split in the same was as in
    :class:`CapSizeCreationPlot` but the each capability is assigned to
    a memory-mapped region based on its offset when it is dereferenced.
    Note that there is an amount of overcounting due to locations that
    are heavily accessed.
    """

    def _build_histogram_input(self):
        # indexes in the vmmap and in the norm_histograms are
        # the same.
        vm_entries = list(self.vmmap)
        # hist_data = [[] for _ in range(len(vm_entries))]
        hist_data = np.empty(len(vm_entries), dtype=object)
        hist_data[:] = [[] for _ in range(len(vm_entries))]

        progress = ProgressPrinter(self.graph.num_vertices(),
                                   desc="Sorting capability references")
        # the loop checks, for each vertex in the graph, where it is
        # dereferenced and adds the dereferenced capability size to every
        # histogram-input-data relative to the VM map entry where the
        # capability is dereferenced
        limits = np.array([(e.start, e.end) for e in vm_entries])
        for node in self.graph.vertices():
            data = self.graph.vp.data[node]
            addr = np.array(data.deref["addr"])
            hist_idx = map(
                lambda lim: np.logical_and(addr >= lim[0], addr <= lim[1]),
                limits)
            for idx, match in enumerate(hist_idx):
                hist_data[idx].extend([data.cap.length] * len(addr[match]))
            progress.advance()
        progress.finish()
        return hist_data


class CapSizeBoundHistogram(CapSizeHistogram):
    """
    Histogram that takes into account capabilities at creation time.
    The address space is split in chunks according to the VM map of the
    process. For each chunk, the set of capabilities that can be
    dereferenced in the chunk is computed. Note that the same capability may
    be counted in multiple chunks if it spans multiple VM map entries (eg DDC)
    From each set an histogram is generated and the bin count is used to produce
    the bar chart.
    """

    def _build_histogram_input(self):
        # indexes in the vmmap and in the norm_histograms are
        # the same.
        vm_entries = list(self.vmmap)
        hist_data = np.empty(len(vm_entries), dtype=object)
        hist_data[:] = [[] for _ in range(len(vm_entries))]
        progress = ProgressPrinter(self.graph.num_vertices(),
                                   desc="Sorting capability references")
        # the loop checks, for each vertex in the graph, whether the
        # capability can be dereferenced in a VM map entry,
        # if so it adds the capability size to the histogram-input-data
        # relative to the VM map entry.
        limits = np.array([(e.start, e.end) for e in vm_entries])
        for node in self.graph.vertices():
            data = self.graph.vp.data[node]
            match = np.logical_and(data.cap.base <= limits[:,1],
                                   data.cap.bound >= limits[:,0])
            for in_data in hist_data[match]:
                in_data.append(data.cap.length)
            progress.advance()
        progress.finish()
        return hist_data


class HistogramPatchBuilder(PatchBuilder):
    """
    Process an histogram to produce the bar plot
    """

    def __init__(self, figure, **kwargs):
        super().__init__(**kwargs)

        self.fig = figure

        self.label_managers = []
        """Manage vertical labels for each vertical column"""

        self.colormap = []
        """Set of colors to use."""

        self.hist = None
        """Histogram model"""

    def inspect(self, hist):
        """Prepare the data for plotting on the axes."""
        assert self.hist == None, \
            "This patch builder can process only a single histogram"
        self.hist = hist
        self.colormap = [plt.cm.Dark2(i) for i in
                         np.linspace(0, 0.9, len(hist.n_bins))]

    def _get_positions(self):
        """X locations of the histogram bars."""
        step = 2
        return range(1, step * self.hist.norm_histogram.shape[0] + 1, step)

    def get_legend(self, handles):
        legend_handles = []
        bin_start = 0
        # skip the first column that holds the vmmap entry for the row
        # labels are set to "2^<bin_stat_size>-2^<bin_end_size>"
        for idx,bin_limit in enumerate(self.hist.norm_histogram.columns):
            label = "2^%d-2^%d" % (bin_start, bin_limit)
            bin_start = bin_limit
            handle = Patch(color=self.colormap[idx], label=label)
            legend_handles.append(handle)
        return legend_handles

    def get_bbox(self):
        positions = list(self._get_positions())
        return Bbox.from_extents(0, 0, positions[-1] + 1, 1.1)

    def get_xticks(self):
        return self._get_positions()

    def get_xlabels(self):
        ticklabels = []
        for entry in self.hist.norm_histogram.index:
            if entry.path:
                label_name = os.path.basename(entry.path)
            else:
                label_name = "0x%x" % entry.start
            ticklabel = "(%s) %s" % (entry.perms, label_name)
            ticklabels.append(ticklabel)
        return ticklabels

    def get_yticks(self):
        return [0, 1]

    def get_ylabels(self):
        return ["0", "100"]

    def get_patches(self, axes):
        norm_hist = self.hist.norm_histogram
        abs_hist = self.hist.abs_histogram
        step = 2
        positions = range(1, step * norm_hist.shape[0] + 1, step)
        # init label managers and legend list
        for row in range(norm_hist.shape[0]):
            mgr = LabelManager(direction="vertical")
            mgr.set_limits(0, np.inf)
            self.label_managers.append(mgr)
        # build the bars in the plot
        bottom = np.zeros(norm_hist.shape[0])
        for bin_idx, bin_limit in enumerate(norm_hist.columns):
            color = self.colormap[bin_idx]
            bar_slices = axes.bar(positions, norm_hist[bin_limit],
                                     bottom=bottom, color=color)
            bottom = bottom + norm_hist[bin_limit]
            # create text labels
            for bar_idx,hist_idx in enumerate(norm_hist.index):
                bar = bar_slices[bar_idx]
                abs_bin = abs_hist.at[hist_idx, bin_limit]
                # write the absolute count count at the left of each bar
                text_x = bar.get_x() - bar.get_width() / 2
                text_y = bar.get_y() + bar.get_height() / 2
                txt = AutoText(text_x, text_y, " %d " % abs_bin,
                               ha="center", va="center",
                               rotation="horizontal",
                               label_manager=self.label_managers[bar_idx])
                axes.add_artist(txt)


class PtrSizePlotDriver(VMMapPlotDriver, ExternalLegendTopPlotBuilder):

    histogram_builder_class = None

    def _get_xlabels_kwargs(self):
        kw = super()._get_xlabels_kwargs()
        kw["rotation"] = "vertical"
        return kw

    def run(self):
        pgm = self._pgm_list[0]
        hist = self.histogram_builder_class(pgm.prov_view(), self._vmmap)
        self.register_patch_builder([hist], HistogramPatchBuilder(self.fig))
        self.process(out_file=self.config.outfile)


class PtrSizeDerefDriver(PtrSizePlotDriver):

    title = "Capability dereference size by memory region"
    x_label = ""
    y_label = ""
    histogram_builder_class = CapSizeDerefHistogram


class PtrSizeBoundDriver(PtrSizePlotDriver):

    title = "Capability bound size by memory region"
    x_label = ""
    y_label = ""
    histogram_builder_class = CapSizeBoundHistogram


class PtrBoundCdf:
    """
    Model of the CDF that the PatchBuilder can draw
    """

    def __init__(self, pgm, absolute=False, graph=None):
        self.pgm = pgm
        """The graph manager"""

        if graph is None:
            self.graph = pgm.prov_view()
        else:
            self.graph = graph
        """The provenance graph."""

        self.size_cdf = None
        """2xn numpy array containing [sizes, frequency]"""

        self.name = pgm.name
        """The CDF name"""

        self.pretend_maps = []
        """
        List of vertex properties used to simulate a different size for matching vertices.
        [(vertex_map, new_base, new_bound), ..]
        """

        self.num_ignored = 0
        """Number of vertices that matched the ignore condition."""

        self.slice_name = None
        """Name of the slice of capabilities that make up this cdf, used for legend"""

        self.absolute = absolute
        """Do not normalize the number of capabilities on the y axis."""

    def pretend_mask(self, mask, invalid_value, force_base=None,
                    force_bound=None):
        self.pretend_maps.append((mask, invalid_value, force_base, force_bound))

    def _check_ignore(self, v):
        """
        Check if a vertex base and bound should be ignored and set to
        something else.
        Return the new base and length, or None
        """
        for ignore_mask, invalid, base, bound in self.pretend_maps:
            if ignore_mask[v] != invalid:
                self.num_ignored += 1
                if base is None or bound is None:
                    u = self.pgm.graph.vertex(ignore_mask[v])
                    u_data = self.pgm.data[u]
                    return u_data.cap.base, u_data.cap.bound
                else:
                    return base, bound
        return None

    def build_cdf(self):
        ptr_sizes = []
        with ProgressTimer("Build CDF"):
            for v in self.graph.vertices():
                vdata = self.graph.vp.data[v]
                effective_bounds = self._check_ignore(v)
                if effective_bounds is None:
                    size = vdata.cap.length
                else:
                    size = effective_bounds[1] - effective_bounds[0]
                ptr_sizes.append(size)
            size_freq = stats.itemfreq(ptr_sizes)
            #logger.debug(size_freq)
            if not self.absolute:
                size_pdf = size_freq[:,1] / len(ptr_sizes)
            else:
                size_pdf = size_freq[:,1]
            if len(size_freq):
                y = np.concatenate(([0, 0], np.cumsum(size_pdf)))
                x = np.concatenate(([0, size_freq[0,0]], size_freq[:,0]))
                self.size_cdf = np.column_stack((x,y))
            else:
                self.size_cdf = np.zeros((1,2))


class BaselineCdf:
    """
    Model of the baseline cdf line.
    """

    MAX_ADDR = 0xffffffffffffffff
    MAX_UADDR = 0x10000000000

    def __init__(self):
        self.name = "baseline"
        self.size_cdf = np.array([
            [0, 0], [self.MAX_UADDR, 0],
            [self.MAX_UADDR, 1], [self.MAX_ADDR, 1]])
        self.num_ignored = -1


class CdfPatchBuilder(PatchBuilder):
    """
    Plot a Cumulative Distribution Function of the number of
    instantiations of capability pointer vs. the capability
    lengths.
    """

    def __init__(self, absolute=False, **kwargs):
        super().__init__(**kwargs)

        self.cdf = []
        """Set of cdf to draw"""

        self.colors = []
        """Colors to use for the lines"""

        self.absolute = absolute
        """Do not normalize the count of capabilities on the Y axis"""

        self._bbox = Bbox.from_extents(1, 0, 0, 1)
        """Bbox of the plot"""

        self.norm_axes = None
        """Optionally, twin axes with the normalized scale, only if absolute"""

    def inspect(self, cdf):
        self.cdf.append(cdf)
        self._bbox.x1 = max(self._bbox.xmax, max(cdf.size_cdf[:,0]))
        self._bbox.y1 = max(self._bbox.ymax, max(cdf.size_cdf[:,1]))

    def get_patches(self, axes):
        c_map = plt.get_cmap("tab20")
        c_norm = colors.Normalize(vmin=0, vmax=len(self.cdf))
        scalar_map = colormap.ScalarMappable(norm=c_norm, cmap=c_map)
        for idx, cdf in enumerate(self.cdf):
            color = scalar_map.to_rgba(idx)
            self.colors.append(color)
            axes.plot(cdf.size_cdf[:,0], cdf.size_cdf[:,1], color=color, lw=2.0)

    def get_legend(self, handles):
        handles = []
        for cdf, color in zip(self.cdf, self.colors):
            if cdf.num_ignored >= 0:
                label = "{} ({:d})".format(cdf.name, cdf.num_ignored)
            elif cdf.slice_name is not None:
                label = "{}-{}".format(cdf.name, cdf.slice_name)
            else:
                label = "{}".format(cdf.name)

            handle = Patch(color=color, label=label)
            handles.append(handle)
        return handles

    def get_bbox(self):
        return self._bbox


class PtrSizeCdfDriver(TaskDriver, ExternalLegendTopPlotBuilder):

    title = "CDF of the size of capabilities created"
    x_label = "Size"
    y_label = "Proportion of the total number of capabilities"

    outfile = Option(help="Output file", default="ptrsize_cdf.pdf")
    publish = Option(help="Adjust plot for publication", action="store_true")
    absolute = Option(help="Do not normalize y axis, show absolute count"
                      " of capabilities", action="store_true")

    filters = Option(
        default=[],
        action="append",
        nargs="+",
        choices=("stack", "mmap", "malloc"),
        help="set of possible elements to modify for the CDF, assume"
        "that the size of the given elements is the maximum possible.")

    split = Option(
        default=[],
        action="append",
        choices=("stack", "stack-all", "malloc", "exec",
                 "glob", "kern", "caprelocs", "caprelocs-only"),
        help="Separate the given vertices in a separate CDF")


    def __init__(self, pgm_list, vmmap, **kwargs):
        super().__init__(**kwargs)
        self.pgm_list = pgm_list
        """List of graph managers to plot."""

        self.vmmap = vmmap
        """VMmap model of the process memory mapping."""

        self.datasets = []

        if self.config.publish:
            self._style["font"] = FontProperties(size=25)

        if self.config.absolute:
            self.y_label = "Number of capabilities"

        self.title += " in {}".format(",".join([pgm.name for pgm in self.pgm_list]))

    def _get_title_kwargs(self):
        kw = super()._get_title_kwargs()
        if self.config.publish:
            # suppress the title so we have more space
            kw.update({"visible": False})
        return kw

    def _get_savefig_kwargs(self):
        kw = super()._get_figure_kwargs()
        kw["dpi"] = 300
        return kw

    def _get_axes_rect(self):
        if self.config.publish:
            return [0.125, 0.08, 0.85, 0.8]
        return super()._get_axes_rect()

    def _get_legend_kwargs(self):
        kw = super()._get_legend_kwargs()
        kw["loc"] = "lower right"
        return kw

    def make_plot(self):
        super().make_plot()
        self.ax.set_xscale("log", basex=2)

    def _make_cdf_dataset(self, pgm, vfilt, setname):
        if vfilt is not None:
            view = GraphView(pgm.graph, vfilt=vfilt)
            cdf = PtrBoundCdf(pgm, self.config.absolute, graph=view)
        else:
            cdf = PtrBoundCdf(pgm, self.config.absolute)
        cdf.num_ignored = -1
        cdf.name = setname
        cdf.slice_name = None
        cdf.build_cdf()
        self.datasets.append(cdf)

    def _get_legend_kwargs(self):
        """
        Change layout of the number of colums in the legend
        """
        kw = super()._get_legend_kwargs()
        kw.update({
            "bbox_to_anchor": (-0.125, 1.009, 1.125, 0.102),
            "ncol": 6,
            "labelspacing": 0.5,
            "borderpad": 0.1
        })
        return kw

    def run(self):
        for idx, pgm in enumerate(self.pgm_list):
            self._make_cdf_dataset(pgm, None, "all")

            for split_set in self.config.split:
                if split_set == "stack":
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_usr_stack,
                                           split_set)
                elif split_set == "stack-all":
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_usr_stack,
                                           "stack")
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_stack,
                                           "stack-deref")
                elif split_set == "malloc":
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_malloc,
                                           split_set)
                elif split_set == "exec":
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_exec,
                                           split_set)
                elif split_set == "caprelocs":
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_capreloc,
                                           "relocs")
                elif split_set == "caprelocs-only":
                    difference = pgm.graph.new_vertex_property("bool")
                    difference.a = (pgm.graph.vp.annotated_capreloc.a &
                                    ~pgm.graph.vp.annotated_globptr.a)                    
                    self._make_cdf_dataset(pgm, difference, "relocs-only")
                elif split_set == "glob":
                    # any global pointers or pointers derived from global pointers
                    combined = pgm.graph.new_vertex_property("bool")
                    combined.a = (pgm.graph.vp.annotated_globptr.a |
                                  pgm.graph.vp.annotated_globderived.a)
                    self._make_cdf_dataset(pgm, combined, split_set)
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_captblptr, "captbl")
                elif split_set == "kern":
                    # kernel originated and syscall originated vertices
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_ksyscall, "syscall")
                    self._make_cdf_dataset(pgm, pgm.graph.vp.annotated_korigin, "kern")
                else:
                    logger.error("Invalid --split option value %s", split_set)
                    raise ValueError("Invalid --split option value")

        self.register_patch_builder(
            self.datasets, CdfPatchBuilder(self.config.absolute))
        self.process(out_file=self.config.outfile)
