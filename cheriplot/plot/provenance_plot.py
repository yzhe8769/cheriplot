"""
Copyright 2016 Alfredo Mazzinghi

Copyright and related rights are licensed under the BERI Hardware-Software
License, Version 1.0 (the "License"); you may not use this file except
in compliance with the License.  You may obtain a copy of the License at:

http://www.beri-open-systems.org/legal/license-1-0.txt

Unless required by applicable law or agreed to in writing, software,
hardware and materials distributed under this License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
express or implied.  See the License for the specific language governing
permissions and limitations under the License.


Plot representation of a CHERI pointer provenance tree
"""

import numpy as np
import logging

from io import StringIO

from matplotlib import pyplot as plt
from matplotlib import lines, collections, transforms, patches

from ..utils import ProgressPrinter
from ..core import RangeSet, Range

from ..core import CallbackTraceParser, Instruction
from ..plot import Plot, PatchBuilder
from ..provenance_tree import (CachedProvenanceTree,
                                                CheriCapNode)

logger = logging.getLogger(__name__)

class PointerProvenanceParser(CallbackTraceParser):

    class RegisterSet:
        """
        Extended register set that keeps track of memory
        operations on capabilities.

        We need to know where a register value has been read from
        and where it is stored to. The first is used to infer
        the correct CapNode to add as parent for a new node,
        the latter allows us to set the CapNode.address for
        a newly allocated capability.
        """
        def __init__(self):
            self.reg_nodes = np.empty(32, dtype=object)
            """CheriCapNode associated with each register."""
            self.memory_map = {}
            """CheriCapNodes stored in memory."""

        def __getitem__(self, idx):
            return self.reg_nodes[idx]

        def __setitem__(self, idx, val):
            self.load(idx, val)

        def load(self, idx, node):
            """
            Associate a CheriCapNode to a register that
            contains the capability associated with it.
            """
            self.reg_nodes[idx] = node

        def move(self, from_idx, to_idx):
            """
            When a capability is moved or modified without changing
            bounds the node is propagated to the destination register.
            """
            self.reg_nodes[to_idx] = self.reg_nodes[from_idx]

        def __repr__(self):
            dump = StringIO()
            dump.write("RegisterSet snapshot:\n")
            for idx, node in enumerate(self.reg_nodes):
                if node:
                    dump.write("$c%d = b:0x%x l:0x%x o:0x%x t:%d\n" % (
                        idx, node.base, node.length, node.offset, node.t_alloc))
                else:
                    dump.write("$c%d = Not mapped\n" % idx)


    def __init__(self, dataset, trace):
        super(PointerProvenanceParser, self).__init__(dataset, trace)
        self.regs_valid = False
        """
        Flag used to disable parsing until the registerset 
        is completely initialised.
        """

        self.regset = self.RegisterSet()
        """
        Register set that maps capability registers
        to nodes in the provenance tree.
        """

    def scan_eret(self, inst, entry, regs, last_regs, idx):
        """
        Detect the first eret that enters the process code
        and initialise the register set and the roots of the tree.
        """
        if self.regs_valid:
            return False
        self.regs_valid = True
        logger.debug("Scan initial register set")
        for idx in range(0, 32):
            cap = regs.cap_reg[idx]
            valid = regs.valid_caps[idx]
            if valid:
                node = self.make_root_node(idx, cap)
            else:
                logger.warning("c%d not in initial set", idx)
                if idx == 30:
                    node = self.make_root_node(idx, None)
                    node.base = 0
                    node.offset = 0
                    node.length = 0xffffffffffffffff
                    logger.warning("Guessing KDC %s", node)
        return False

    def scan_csetbounds(self, inst, entry, regs, last_regs, idx):
        """
        Each csetbounds is a new pointer allocation
        and is recorded as a new node in the provenance tree.
        """
        if not self.regs_valid:
            return False
        node = self.make_node(entry, inst)
        node.origin = CheriCapNode.C_SETBOUNDS
        self.regset.load(inst.cd.cap_index, node) # XXX scan_cap
        return False

    def scan_cfromptr(self, inst, entry, regs, last_regs, idx):
        """
        Each cfromptr is a new pointer allocation and is
        recodred as a new node in the provenance tree.
        """
        if not self.regs_valid:
            return False
        node = self.make_node(entry, inst)
        node.origin = CheriCapNode.C_FROMPTR
        self.regset.load(inst.cd.cap_index, node) # XXX scan_cap
        return False

    def scan_cap(self, inst, entry, regs, last_regs, idx):
        """
        Whenever a capability instruction is found, update
        the mapping from capability register to the provenance
        tree node associated to the capability in it.
        """
        if not self.regs_valid:
            return False
        self.update_regs(inst, entry, regs, last_regs)
        return False

    def scan_clc(self, inst, entry, regs, last_regs, idx):
        """
        If a capability is loaded in a register we need to find
        a node for it or create one. The address map is used to
        lookup nodes that have been stored at the load memory
        address.
        """
        if not self.regs_valid:
            return False

        cd = entry.capreg_number()
        try:
            node = self.regset.memory_map[entry.memory_address]
        except KeyError:
            logger.debug("Load c%s from new location 0x%x, create root node",
                         cd, entry.memory_address)
            node = None

        if node is None:
            # add a node as a root node because we have never
            # seen the content of this register yet
            node = self.make_root_node(cd, inst.cd.value)
            logger.debug("Found %s value (missing in initial set) %s",
                         inst.cd.name, node)
        self.regset[cd] = node
        return False

    def scan_csc(self, inst, entry, regs, last_regs, idx):
        """
        Record the locations where a capability node is stored
        """
        if not self.regs_valid:
            return False
        cd = entry.capreg_number()
        node = self.regset[cd]
        if node is None and not last_regs.valid_caps[cd]:
            # add a node as a root node because we have never
            # seen the content of this register yet
            node = self.make_root_node(cd, inst.cd.value)
            logger.debug("Found %s value (missing in initial set)",
                         inst.cd.name, node)

            # XXX for now return but the correct behaviour would
            # be to recover the capability missing in the initial set
            # from the current entry and create a new root node for it
            return False
        self.regset.memory_map[entry.memory_address] = node
        node.address[entry.cycles] = entry.memory_address
        return False

    def make_root_node(self, idx, cap):
        """
        Create a root node of the provenance tree.
        The node is added to the tree and associated
        with the destination register of the current instruction.

        :param idx: index of the destination capability register for
        the current instruction
        :type idx: int
        :param cap: capability register value
        :type cap: :class:`pycheritrace.capability_register`
        :return: the newly created node
        :rtype: :class:`cheriplot.provenance_tree.CheriCapNode`
        """
        node = CheriCapNode(cap)
        node.t_alloc = 0
        self.dataset.append(node)
        self.regset.load(idx, node)
        return node

    def make_node(self, entry, inst):
        """
        Create a node in the provenance tree.
        The parent is fetched from the register set depending on the source
        registers of the current instruction.
        """
        node = CheriCapNode(inst.cd.value)
        node.t_alloc = entry.cycles
        node.pc = entry.pc
        node.is_kernel = entry.is_kernel()
        # find parent node, if no match then the tree is returned
        try:
            parent = self.regset[inst.cb.cap_index]
        except:
            logger.error("Error searching for parent node of %s", node)
            raise

        if parent == None:
            logger.error("Missing parent c%d [%x, %x]",
                         entry.capreg_number(), src.base, src.length)
            raise Exception("Missing parent for %s [%x, %x]" %
                            (node, src.base, src.length))
        parent.append(node)
        # # check for loops, there should not be any
        # if len(self.dataset.check_consistency([])) != 0:
        #     logger.error("Inconsistent tree build parent @ %d: %s, node %s", entry.cycles, parent, node)
        #     assert False, "Tree consistency violation"
        return node

    def update_regs(self, inst, entry, regs, last_regs):
        cd = inst.cd
        cb = inst.cb
        if (cd is None or cd.cap_index == -1):
            return
        if (cb is None or cb.cap_index == -1):
            return
        self.regset.move(cb.cap_index, cd.cap_index)


class LeafCapPatchBuilder(PatchBuilder):
    """
    The patch generator build the matplotlib patches for each
    capability node and generates the ranges of address-space in
    which we are not interested.


    Generate address ranges that are displayed as shortened in the
    address-space plot based on leaf capabilities found in the
    provenance tree.
    We only care about zones where capabilities without children
    are allocated. If the allocations are spaced out more than
    a given number of pages, the space in between is omitted
    in the plot.
    """

    def __init__(self):
        super(LeafCapPatchBuilder, self).__init__()

        self.split_size = 2 * self.size_limit
        """
        Capability length threshold to trigger the omission of
        the middle portion of the capability range.
        """

        self.y_unit = 10**-6
        """Unit on the y-axis"""

        self._omit_collection = np.empty((1,2,2))
        """Collection of elements in omit ranges"""

        self._keep_collection = np.empty((1,2,2))
        """Collection of elements in keep ranges"""

        self._arrow_collection = []
        """Collection of arrow coordinates"""

    def _build_patch(self, node_range, y):
        """
        Build patch for the given range and type and add it
        to the patch collection for drawing
        """
        line = [[(node_range.start, y), (node_range.end, y)]]
        if node_range.rtype == Range.T_KEEP:
            self._keep_collection = np.append(self._keep_collection, line, axis=0)
        elif node_range.rtype == Range.T_OMIT:
            self._omit_collection = np.append(self._omit_collection, line, axis=0)
        else:
            raise ValueError("Invalid range type %s" % node_range.rtype)

    def _build_provenance_arrow(self, src_node, dst_node):
        """
        Build an arrow that shows the source capability for a node
        The arrow goes from the source to the child
        """
        src_x = (src_node.base + src_node.bound) / 2
        src_y = src_node.t_alloc * self.y_unit
        dst_x = (dst_node.base + dst_node.bound) / 2
        dst_y = dst_node.t_alloc * self.y_unit
        dx = dst_x - src_x
        dy = dst_y - src_y
        arrow = patches.FancyArrow(src_x, src_y, dx, dy,
                                   fc="k",
                                   ec="k",
                                   head_length=0.0001,
                                   head_width=0.0001,
                                   width=0.00001)
        # self._arrow_collection.append(arrow)

    def inspect(self, node):
        # if len(node) != 0:
        #     # not a leaf in the provenance tree
        #     return
        if node.bound < node.base:
            logger.warning("Skip overflowed node %s", node)
            return
        node_y = node.t_alloc * self.y_unit
        node_box = transforms.Bbox.from_extents(node.base, node_y,
                                                node.bound, node_y)

        self._bbox = transforms.Bbox.union([self._bbox, node_box])
        if node.length > self.split_size:
            l_range = Range(node.base, node.base + self.size_limit,
                            Range.T_KEEP)
            r_range = Range(node.bound - self.size_limit, node.bound,
                            Range.T_KEEP)
            omit_range = Range(node.base + self.size_limit,
                               node.bound - self.size_limit,
                               Range.T_OMIT)
            self._update_regions(l_range)
            self._update_regions(r_range)
            self._build_patch(l_range, node_y)
            self._build_patch(r_range, node_y)
            self._build_patch(omit_range, node_y)
        else:
            keep_range = Range(node.base, node.bound, Range.T_KEEP)
            self._update_regions(keep_range)
            self._build_patch(keep_range, node_y)
        # build arrows
        for child in node:
            self._build_provenance_arrow(node, child)

    def get_patches(self):
        omit_patch = collections.LineCollection(self._omit_collection,
                                                linestyle="solid")
        keep_patch = collections.LineCollection(self._keep_collection,
                                                linestyle="solid")
        arrow_patch = collections.PatchCollection(self._arrow_collection)
        return [omit_patch, keep_patch, arrow_patch]


class PointerProvenancePlot(Plot):
    """
    Plot the provenance tree showing the time of allocation vs 
    base and bound of each node.
    """

    def __init__(self, tracefile):
        super(PointerProvenancePlot, self).__init__(tracefile)

        self.patch_builder = LeafCapPatchBuilder()
        """Strategy object that builds the plot components"""

    def init_parser(self, dataset, tracefile):
        return PointerProvenanceParser(dataset, tracefile)

    def init_dataset(self):
        return CachedProvenanceTree()

    def _get_cache_file(self):
        return self.tracefile + self.__class__.__name__ + ".cache"

    def _get_plot_file(self):
        return self.tracefile + ".png"

    def build_dataset(self):
        """
        Build the provenance tree
        """
        logger.debug("Generating provenance tree for %s", self.tracefile)
        try:
            if self._caching:
                fname = self._get_cache_file()
                try:
                    self.dataset.load(fname)
                except IOError:
                    self.parser.parse()
                    self.dataset.save(self._get_cache_file())
            else:
                self.parser.parse()
        except Exception as e:
            logger.error("Error while generating provenance tree %s", e)
            raise

        errs = []
        self.dataset.check_consistency(errs)
        if len(errs) > 0:
            logger.warning("Inconsistent provenance tree: %s", errs)

        num_nodes = len(self.dataset)
        logger.debug("Total nodes %d", num_nodes)
        progress = ProgressPrinter(num_nodes, desc="Remove kernel nodes")
        def remove_nodes(node):
            """
            remove null capabilities
            remove operations in kernel mode
            """
            if (node.offset >= 0xFFFFFFFF0000000 or
                (node.length == 0 and node.base == 0)):
                # XXX should we only check the length?
                node.selfremove()
            progress.advance()
        self.dataset.visit(remove_nodes)
        progress.finish()

        num_nodes = len(self.dataset)
        logger.debug("Filtered kernel nodes, remaining %d", num_nodes)
        progress = ProgressPrinter(num_nodes, desc="Merge (cfromptr + csetbounds) sequences")
        def merge_setbounds(node):
            """
            merge cfromptr -> csetbounds subtrees
            """
            if (node.parent.origin == CheriCapNode.C_FROMPTR and
                node.origin == CheriCapNode.C_SETBOUNDS and
                len(node.parent.children) == 1):
                # the child must be unique to avoid complex logic
                # when merging, it may be desirable to do so with
                # more complex traces
                node.origin = CheriCapNode.C_PTR_SETBOUNDS
                grandpa = node.parent.parent
                node.parent.selfremove()
                grandpa.append(node)
            progress.advance()
        self.dataset.visit(merge_setbounds)
        progress.finish()

    def plot(self):
        """
        Create the provenance plot and return the figure
        """

        fig = plt.figure(figsize=(15,10))
        ax = fig.add_axes([0.05, 0.15, 0.9, 0.80,],
                          projection="custom_addrspace")

        dataset_progress = ProgressPrinter(len(self.dataset), desc="Adding nodes")
        for item in self.dataset:
            self.patch_builder.inspect(item)
            dataset_progress.advance()
        dataset_progress.finish()

        for collection in self.patch_builder.get_patches():
            ax.add_collection(collection)
        ax.set_omit_ranges(self.patch_builder.get_omit_ranges())

        view_box = self.patch_builder.get_bbox()
        xmin = view_box.xmin * 0.98
        xmax = view_box.xmax * 1.02
        ymin = view_box.ymin * 0.98
        ymax = view_box.ymax * 1.02
        logger.debug("X limits: (%d, %d)", xmin, xmax)
        ax.set_xlim(xmin, xmax)
        logger.debug("Y limits: (%d, %d)", ymin, ymax)
        ax.set_ylim(ymin, ymax * 1.02)
        ax.invert_yaxis()

        logger.debug("Plot build completed")
        return fig