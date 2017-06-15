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
import logging
import os

from enum import IntEnum
from functools import reduce
from graph_tool.all import Graph, load_graph

from cheriplot.core import (
    CallbackTraceParser, ProgressTimer, MultiprocessCallbackParser, IClass)
from cheriplot.provenance.model import *
from cheriplot.provenance.transforms import bfs_transform, BFSTransform

logger = logging.getLogger(__name__)

__all__ = ("PointerProvenanceParser", "MissingParentError",
           "DereferenceUnknownCapabilityError", "SubgraphMergeError",
           "UnexpectedOperationError")

class SubgraphMergeError(RuntimeError):
    """
    Exception raised when there is an error during the merge
    of partial results from multiprocessing workers.
    """
    pass


class MissingParentError(RuntimeError):
    """
    Exception raised when attempting to create a provenance node but a
    valid parent is not found.
    This is a fatal error condition.
    """
    pass


class DereferenceUnknownCapabilityError(RuntimeError):
    """
    Exception raised when a capability dereference is found
    but it is not possible to determine the corresponding
    vertex in the graph where the dereference should be registered.
    This happens when a previously unseen capability register is
    dereferenced or in case of bugs in the vertex propagation in
    the register set.
    This is a fatal error condition.
    """
    pass


class UnexpectedOperationError(RuntimeError):
    """
    Exception raised when a seemingly impossible operation
    occurred.
    This is a fatal error condition.
    """
    pass


class VertexMemoryMap:
    """
    Helper object that keeps track of the graph vertex associated
    with each memory location used in the trace.
    """

    def __init__(self, pgm):
        self.vertex_map = {}
        self.pgm = pgm

    def __getstate__(self):
        """
        Make object pickle-able, the graph-tool vertices index are used
        instead of the vertex object.
        """
        logger.debug("Pickling partial result vertex-memory map %d",
                     os.getpid())
        state = {
            "vertex_map": {k: int(v) for k,v in self.vertex_map.items()}
        }
        return state

    def __setstate__(self, data):
        """
        Make object pickle-able, the graph-tool vertices index are used
        instead of the vertex object.
        """
        logger.debug("Unpickling partial result vertex-memory map")
        self.vertex_map = data["vertex_map"]

    def clear(self, addr):
        """
        Unregister the vertex associated with the given memory location
        """
        del self.vertex_map[addr]

    def mem_load(self, addr, vertex=None):
        """
        Register a memory load at given address and return
        the vertex for that address if any.
        If a vertex is specified, the vertex is set
        in the memory map at the given address.
        """
        if vertex:
            self.vertex_map[addr] = vertex
        try:
            return self.vertex_map[addr]
        except KeyError:
            return None

    def mem_store(self, addr, vertex):
        """
        Register a memory store at given address and
        store the vertex in the memory map for the given
        address.
        """
        self.vertex_map[addr] = vertex


class MPVertexMemoryMap(VertexMemoryMap):
    """
    Vertex map used by multiprocessing workers to record the
    initial state of the map so that the initial vertices can
    be merged with the results from other workers.
    """

    def __init__(self, pgm):
        super().__init__(pgm)

        self.initial_map = {}

    def __getstate__(self):
        state = super().__getstate__()
        state["initial_map"] = {k: int(v) for k,v in self.initial_map.items()}
        return state

    def __setstate__(self, data):
        super().__setstate__(data)
        self.initial_map = data["initial_map"]

    def mem_load(self, addr, vertex=None):
        if vertex and addr not in self.initial_map:
            self.initial_map[addr] = vertex
        return super().mem_load(addr, vertex)


class RegisterSet:
    """
    Helper object that keeps track of the graph vertex associated
    with each register in the register file.

    The register set is also used in the subgraph merge
    resolution to produce the full graph from partial
    results from worker processes.
    """

    def __init__(self, pgm):
        self.reg_nodes = [None] * 32
        """Graph node associated with each register."""

        self._pcc = None
        """Current pcc node"""

        self.pgm = pgm
        """The provenance graph manager"""

    def __getstate__(self):
        """
        Make object pickle-able, graph-tool vertices are not pickleable
        but their index is.
        """
        logger.debug("Pickling partial result register set %d", os.getpid())
        state = {
            "reg_nodes": [self.pgm.graph.vertex_index[u] if u != None else None
                          for u in self.reg_nodes],
            "_pcc": (self.pgm.graph.vertex_index[self._pcc]
                     if self._pcc != None else None),
            }
        return state

    def __setstate__(self, data):
        """
        Make object pickle-able.

        Restore internal state. Note that this does not recover the vertex
        instances from the graph as we do not require this when propagating
        partial results from the workers.
        XXX Doing so saves some time although it may be desirable to
        perform the operation to avoid confusion.
        Note also that the graph is dropped, this is to avoid pickling the
        graph twice.
        """
        logger.debug("Unpickling partial result register set")
        self.reg_nodes = data["reg_nodes"]
        self._pcc = data["_pcc"]

    def _attach_subgraph_merge(self, regset_vertex, input_vertex):
        """
        If needed, attach a graph vertex to a
        partial-vertex marker in the register set

        :param regset_vertex: the vertex currently contained in the
        register set
        :param input_vertex: the vertex that is being assigned
        """
        # if the node is a root and we have a PARTIAL dummy node in the register
        # set and the node is not already attached to a PARTIAL dummy node,
        # the root is attached to the dummy.
        if input_vertex == None or regset_vertex == None:
            return

        in_data = self.pgm.data[input_vertex]
        if in_data.origin == CheriNodeOrigin.ROOT:
            for n in input_vertex.in_neighbours():
                if self.pgm.data[n].origin == CheriNodeOrigin.PARTIAL:
                    return
            curr_data = self.pgm.data[regset_vertex]
            if curr_data.origin == CheriNodeOrigin.PARTIAL:
                self.pgm.graph.add_edge(regset_vertex, input_vertex)

    @property
    def pcc(self):
        return self._pcc

    @pcc.setter
    def pcc(self, value):
        self._attach_subgraph_merge(self._pcc, value)
        self._pcc = value

    def has_pcc(self, allow_root=False):
        """
        Check if the register set contains a valid pcc

        :param idx: the register index to check
        :param allow_root: a root can be created if the register
        does not have a valid node.
        """
        if self.pcc == None:
            return False
        if allow_root:
            data = self.pgm.data[self.pcc]
            if data.origin == CheriNodeOrigin.PARTIAL:
                return False
        return True

    def has_reg(self, idx, allow_root=False):
        """
        Check if the register set contains a valid entry for
        the given register index

        :param idx: the register index to check
        :param allow_root: a root can be created if the register
        does not have a valid node.
        """
        assert idx < 32, "Out of bound register set index"
        if self[idx] == None:
            return False
        if allow_root:
            data = self.pgm.data[self[idx]]
            if data.origin == CheriNodeOrigin.PARTIAL:
                return False
        return True

    def __getitem__(self, idx):
        """
        Fetch the :class:`cheriplot.core.provenance.GraphNode`
        currently associated to a capability register with the
        given register number.
        """
        assert idx < 32, "Out of bound register set fetch"
        return self.reg_nodes[idx]

    def __setitem__(self, idx, val):
        """
        Fetch the :class:`cheriplot.core.provenance.GraphNode`
        currently associated to a capability register with the
        given register number.
        """
        assert idx < 32, "Out of bound register set assignment"
        # if the current value of the register set is short-lived
        # (never stored anywhere and not in any other regset node)
        # then it is effectively lost and "deallocated"
        # if self.reg_nodes[idx] is not None:
        #     n_refs = np.count_nonzero(self.reg_nodes == self.reg_nodes[idx])
        #     node_data = self.pgm.data[self.reg_nodes[idx]]
        #     # XXX may refine this by checking the memory_map to see if the
        #     # node is still there
        #     n_refs += len(node_data.address)
        #     if n_refs == 1:
        #         # can safely set the t_free
        #         disable because we need a way to actually get the current cycle
        self._attach_subgraph_merge(self.reg_nodes[idx], val)
        self.reg_nodes[idx] = val


class MergePartialSubgraphContext:
    """
    Hold the context information for the merge subgraph transform
    steps.
    """

    def __init__(self, main_pgm):

        self.pgm = main_pgm
        """Merged graph manger."""

        self.pgm_subgraph = None
        """Current subgraph pgm being merged."""

        self.prev_regset = None
        """Previous step final regset."""

        self.prev_vmap = None
        """Previous step vinal vertex-memory map."""

        self.prev_pcc_fixup = None
        """Previous step PccFixup subparser result."""

        self.prev_syscall = None
        """Previous step syscall subparser result."""

        self.curr_regset = None
        """Current step final regset."""

        self.curr_initial_regset = None
        """Current step initial regset."""

        self.curr_vmap = None
        """Current step vertex-memory map."""

        self.curr_pcc_fixup = None
        """Current step PccFixup subparser result."""

        self.curr_syscall = None
        """Current step syscall subparser result."""

        self.step_idx = 0
        """Merge step index."""

    def _merge_graph_properties(self):
        """Merge global properties of the graph"""
        if self.step_idx == 0:
            # copy the stack graph property from the first worker
            logger.debug("Merge initial stack: %s", self.pgm_subgraph.stack)
            self.pgm.stack = self.pgm_subgraph.stack

    def step(self, result):
        """
        Process a merge step
        """
        # copy the graph into the merged dataset and
        # merge the root nodes from the initial register set
        # with the previous register set
        self.pgm_subgraph = result["pgm"]
        self.curr_initial_regset = result["initial_regset"]
        self.curr_regset = result["final_regset"]
        self.curr_vmap = result["mem_vertex_map"]
        self.curr_pcc_fixup = result["sub_pcc_fixup"]
        self.curr_syscall = result["sub_syscall"]
        self._merge_graph_properties()
        transform = MergePartialSubgraph(self)
        bfs_transform(result["pgm"].graph, [transform])
        transform.finalize()
        self.step_idx += 1


class MergePartialSubgraph(BFSTransform):
    """
    Merge a partial subgraph into the main graph.

    This is used to merge partial results from
    multiprocessing workers that parse the
    provenance graph.

    The transform must be run on the subgraph that is
    should be merged.
    """

    def __init__(self, context):
        self.context = context

        if self.subgraph:
            self.copy_vertex_map = self.subgraph.new_vertex_property(
                "long", val=-1)
            """
            Map vertex index in the subgraph to a vertex index in the
            merged graph.
            """

            self.omit_vertex_map = self.subgraph.new_vertex_property(
                "bool", val=False)
            """
            Mark vertices in the subgraph that should be ignored when moving
            to the merged graph.
            """

    @property
    def graph(self):
        """Merged graph that we are building."""
        return self.context.pgm.graph

    @property
    def subgraph(self):
        """Subgraph to be merged."""
        return self.context.pgm_subgraph.graph

    @property
    def initial_regset(self):
        """
        Regset mapping initial register state to vertices in the
        subgraph.

        Note: the register set contains vertex handles for the subgraph.
        """
        return self.context.curr_initial_regset

    @property
    def final_regset(self):
        """
        Regset mapping the final register state of the worker to the
        vertices in the merged graph. This is generated from the final
        regset given to the transform, which contains vertex handles for
        the subgraph.
        """
        return self.context.curr_regset

    @property
    def previous_regset(self):
        """
        Regset mapping the final state of the registers of the last merged
        subgraph to vertices in the merged graph.

        Note: the register set contains vertex handles for the merged graph.
        """
        return self.context.prev_regset

    @property
    def vertex_map(self):
        """
        VertexMemoryMap with the initial and final state of graph
        vertices in memory after the subgraph has been parsed.

        Note: the map contains vertex handles from the subgraph.
        """
        return self.context.curr_vmap

    @property
    def previous_vmap(self):
        """
        VertexMemoryMap with the final state of graph vertices in memory
        from the previous subgraph merge step.

        Note: the map contains vertex handles from the merged graph.
        """
        return self.context.prev_vmap

    def finalize(self):
        """
        Finalize the context for this merge step
        """
        self.context.prev_regset = self.get_final_regset()
        self.context.prev_vmap = self.get_final_vmap()
        self.context.prev_pcc_fixup = self.get_final_pcc_fixup()
        self.context.prev_syscall = self.get_final_syscall()

    def get_final_syscall(self):
        """
        Return the syscall subparser state to be used in the next merge step
        """
        sys_result = dict(self.context.curr_syscall)
        if sys_result["eret_cap"] != None and self.context.step_idx > 0:
            # translate the saved vertex to an index in the merged graph
            v = self.copy_vertex_map[sys_result["eret_cap"]]
            if v < 0:
                msg = "syscall return capability is not copied"\
                      " to merged graph, it must be a PARTIAL"
                # we may search for a candidate ROOT vertex but for
                # now fail, this should never be happening?
                logger.error(msg)
                raise SubgraphMergeError(msg)
            sys_result["eret_cap"] = v
        return sys_result

    def get_final_pcc_fixup(self):
        """
        Return the pcc fixup state to be used by the next merge step
        """
        pcc_fixup = dict(self.context.curr_pcc_fixup)
        if pcc_fixup["saved_pcc"] != None:
            # translate the saved vertex to an index in the merged graph
            v_pcc = self.copy_vertex_map[pcc_fixup["saved_pcc"]]
            if v_pcc < 0:
                msg = "final pcc fixup saved_pcc is not copied"\
                      " to merged graph, it must be a PARTIAL"
                # we may search for a candidate ROOT vertex but for
                # now fail, it is not clear how often this should be happening
                logger.error(msg)
                raise SubgraphMergeError(msg)
            pcc_fixup["saved_pcc"] = v_pcc
        return pcc_fixup

    def get_final_regset(self):
        """
        Return the register set mapping the final state of the worker
        partial graph to vertices in the merged graph (that have been
        generated during this merge step).
        This is used as input to the next merge operation as previous_regset.
        """
        regset = RegisterSet(None)
        for idx in range(len(self.final_regset.reg_nodes)):
            v = self.final_regset[idx]
            if v == None or self.copy_vertex_map[v] < 0:
                regset.reg_nodes[idx] = None
            else:
                regset.reg_nodes[idx] = self.copy_vertex_map[v]

        v_pcc = self.final_regset.pcc
        regset.pcc = self.copy_vertex_map[v_pcc] if v_pcc != None else None
        logger.debug(regset.reg_nodes)
        logger.debug(regset.pcc)
        return regset

    def get_final_vmap(self):
        """
        Return the vertex-memory-map containing the final state of
        the worker partial graph expressed with vertices in the merged graph
        (that have been generated during this merge step).
        This is used as input to the next merge operation as previous_vmap.
        """
        vmap = VertexMemoryMap(None)
        for key,u in self.vertex_map.vertex_map.items():
            if self.copy_vertex_map[u] >= 0:
                # valid vertex handle
                vmap.vertex_map[key] = self.copy_vertex_map[u]
        return vmap

    def _merge_partial_vertex_data(self, u_data, v_data):
        """
        Copy dereferences and stores from a subgraph dummy vertex
        to a vertex in the merged graph.

        :param u_data: the source vertex data
        :param v_data: the destination vertex data
        """
        for key, val in u_data.events.items():
            v_data.events[key].extend(val)

    def _check_cap_compatible(self, u_data, v_data):
        """
        Check if two capability vertex data are compatible for
        merging/suppression.
        If they are similar enough there are some cases in which they
        actually represent the same thing.
        Used in merge decisions for ROOT vertices.
        """
        return not (u_data.cap.base != v_data.cap.base or
                    u_data.cap.length != v_data.cap.length or
                    u_data.cap.permissions != v_data.cap.permissions or
                    u_data.cap.objtype != v_data.cap.objtype)

    def _merge_initial_vertex(self, u):
        """
        Merge a vertex that is contained in the initial register set.
        In this case u is a dummy vertex used only for marking.
        """
        # case (1) (2) in examine_vertex.
        # Do not add the dummy vertex to the copy_vertex_map
        # so the edges PARTIAL -> ROOT are not moved and
        # only ROOT vertices are moved normally when they are
        # found in case (3) of examine_vertex.

        if self.context.step_idx == 0:
            # there is no previous regset
            logger.debug("Merge trace beginning initial vertex subgraph:%d", u)
            self._merge_trace_beginning(u)
        else:
            if u in self.initial_regset.reg_nodes:
                index = self.initial_regset.reg_nodes.index(u)
                v = self.previous_regset[index]
            else:
                index = "pcc"
                v = self.previous_regset.pcc
            logger.debug("Merge initial vertex (register %s)", index)

            if v is None or v < 0:
                # no corresponding parent
                self._merge_initial_vertex_to_none(u)
            else:
                self._merge_initial_vertex_to_prev(u, v)

    def _merge_initial_mem_vertex(self, u):
        """
        Merge a vertex that is contained in the initial vertex
        memory map of a worker.

        There are 2 cases:
        i) the previous vertex map do not have anything at the given address.
        Note this is weird but possible store at that location was never seen,
        so we give a warning but perform the merge.
        ii) the previous vertex map have something stored at the location.
        Then the previous vertex and the current ROOT vertex must be compatible,
        otherwise it is an error. If they are compatible suppress the ROOT.
        """
        u_addr = None
        # XXX suboptimal way of searching, hopefully it's not too bad
        # depends on the working set in memory though. The probelm here
        # is that we want fast lookup on the address key but also on the
        # mapped value
        for key,val in self.vertex_map.initial_map.items():
            if val == u:
                u_addr = key
                break
        try:
            v = self.previous_vmap.vertex_map[u_addr]
        except KeyError:
            # merge the root vertex normally
            logger.warning("Parent memory vertex not found in merged"
                           "vertex map @ 0x%x, subgraph:%s", u_addr, u)
            self._merge_subgraph_vertex(u)
        else:
            # suppress the root vertex
            u_data = self.subgraph.vp.data[u]
            v_data = self.graph.vp.data[v]
            if not self._check_cap_compatible(u_data, v_data):
                msg = "Incompatible vertex in prev_vmap at address 0x%x,"\
                      " curr:%s prev:%s" % (u_addr, u_data, v_data)
                # this is an error, the worker found something inconsistent
                # in the trace for this memory address.
                logger.error(msg)
                raise SubgraphMergeError(msg)
            else:
                # the root can be merged with the prev_vmap content
                self.copy_vertex_map[u] = v
                self._merge_partial_vertex_data(u_data, v_data)

    def _merge_trace_beginning(self, u):
        """
        Merge an initial vertex in the first subgraph. In this
        case there is no previous subparser because this comes from
        the first chunk of the trace.
        Case (4) in examine_vertex:
        The dummy register is attached to something unknown, the only
        way to know the value of the register is by looking at ROOT
        vertices that have been attached to the dummy.

        If there are ROOT vertices, then they must be from the *same*
        previous vertex, because ROOT vertices are attached to the
        PARTIAL vertex only as long as they would not replace it.

        The multiple ROOT vertices are merged and all the successors
        of the PARTIAL vertex are attached to the merged vertex.
        """
        u_data = self.subgraph.vp.data[u]
        merged_root = None
        roots = []
        other = []
        for u_out in u.out_neighbours():
            u_out_data = self.subgraph.vp.data[u_out]
            if u_out_data.origin == CheriNodeOrigin.ROOT:
                roots.append(u_out)
            else:
                other.append(u_out)
        if len(roots):
            # merge all the roots in a single one
            merged_root = self.graph.add_vertex()
            for idx,v in enumerate(roots):
                if idx == 0:
                    # first step, use the first root data
                    root_data = self.subgraph.vp.data[v]
                    self.graph.vp.data[merged_root] = root_data
                else:
                    # if the root is compatible (have same bounds & perms)
                    # merge the data into the main vertex
                    v_data = self.subgraph.vp.data[v]
                    if not self._check_cap_compatible(root_data, v_data):
                        msg = "Can not merge ROOTs at trace beginning:"\
                              " incompatible roots %s %s" % (root_data, v_data)
                        logger.error(msg)
                        raise SubgraphMergeError(msg)
                    self._merge_partial_vertex_data(v_data, root_data)
                # ensure that the children of the children will be
                # attached to the merged root and the root vertices
                # are ignored
                self.copy_vertex_map[v] = merged_root
                self.omit_vertex_map[v] = True
        if len(other):
            if len(roots) == 0:
                # XXX we may promote the vertices to roots in
                # this case?
                msg = "Can not resolve parent for children of "\
                      "PARTIAL vertex at trace beginning, "\
                      "the PARTIAL vertex have no associated roots"
                logger.error(msg)
                raise MissingParentError(msg)
            # all the other (non-root) children are attached to the
            # merged root
            self.copy_vertex_map[u] = merged_root

    def _merge_initial_vertex_to_none(self, u):
        """
        Merge an initial vertex that have no parent in
        the previous regset.
        Case (1) in examine_vertex:
        The dummy vertex must not have been dereferenced,
        because this counts as an empty register now.
        it can have been stored, it is just storing None.
        """
        u_data = self.subgraph.vp.data[u]
        # get the length in constant memory instad of O(n) memory
        n_deref = sum(1 for etype in u_data.events["type"]
                      if etype & NodeData.EventType.deref_mask())
        if n_deref:
            raise SubgraphMergeError("PARTIAL vertex was dereferenced "
                                     "but is merged to None")
        logger.debug("initial vertex prev graph:None")
        # XXX why we are not collapsing the roots if they have
        # matching bounds? also if multiple roots are attached
        # this may be a problem?
        for u_out in u.out_neighbours():
            u_out_data = self.subgraph.vp.data[u_out]
            if u_out_data.origin != CheriNodeOrigin.ROOT:
                raise MissingParentError(
                    "Missing parent for %s" % u_out_data)

    def _merge_initial_vertex_to_prev(self, u, v):
        """
        Merge an initial vertex that have an existing parent
        in the previous regset.
        Case (2) of examine_vertex:
        Propagate PARTIAL metadata to the parent.
        Remove ROOT children since the ROOT should not
        have been created.
        """
        u_data = self.subgraph.vp.data[u]
        logger.debug("initial vertex prev graph:%s", v)
        self.copy_vertex_map[u] = v
        v_data = self.graph.vp.data[v]
        self._merge_partial_vertex_data(u_data, v_data)
        for u_out in u.out_neighbours():
            logger.debug("initial vertex out-neighbour subgraph:%s",
                         u_out)
            # check that v_data agrees with all roots
            # that will be suppressed
            u_out_data = self.subgraph.vp.data[u_out]
            if u_out_data.origin == CheriNodeOrigin.ROOT:
                # suppress u_out but attach its children to
                # the dummy so the connectivity is preserved
                # so all dereferences and stores of u_out are merged in the
                # parent
                if len(list(u_out.in_neighbours())) != 1:
                    raise SubgraphMergeError(
                        "ROOT attached to multiple partial nodes")
                self._merge_partial_vertex_data(u_out_data, v_data)
                if not self._check_cap_compatible(u_out_data, v_data):
                    logger.debug("do not suppress ROOT %s, previous "
                                 "regset does not have matching "
                                 "bounds %s", u_out_data, v_data)
                else:
                    self.omit_vertex_map[u_out] = True
                    for w in u_out.out_neighbours():
                        self.subgraph.add_edge(u, w)

    def _merge_subgraph_vertex(self, u):
        """
        Merge a generic vertex from the subgraph to the main merged graph.
        Case (3) of examine_vertex
        """
        v = self.graph.add_vertex()
        self.graph.vp.data[v] = self.subgraph.vp.data[u]
        self.copy_vertex_map[u] = v
        for u_in in u.in_neighbours():
            logger.debug("in-neighbour subgraph:%s", u_in)
            # v_in must exist because we are doing BFS
            # however if u_in is a root v_in is None
            v_in = self.copy_vertex_map[u_in]
            if v_in >= 0:
                logger.debug("valid in-neighbour subgraph:%s", u_in)
                self.graph.add_edge(v_in, v)

    def _merge_pcc_fixup(self, u):
        """
        Merge a vertex that has been marked as initial epcc
        by the PccFixup subparser. This happens when the trace was
        split in the middle of an exception caused by a capability
        branch.
        """
        curr_result = self.context.curr_pcc_fixup
        prev_result = self.context.prev_pcc_fixup

        badvaddr = curr_result["badvaddr"]
        jmp_instr_addr = prev_result["saved_addr"]
        if badvaddr is None and jmp_instr_addr is None:
            # nothing to do
            return

        if badvaddr == jmp_instr_addr or badvaddr == jmp_instr_addr + 4:
            # the PccFixup assumes that the branch instruction
            # always commit, if this is not the case, the badvaddr
            # is the one of the branch instruction and we should
            # restore epcc to its previous value.
            # u == epcc node
            u_data = self.subgraph.vp.data[u]
            if u_data.origin == CheriNodeOrigin.PARTIAL:
                # u is a dummy vertex that will be merged
                # need to replace the corresponding parent with the
                # saved pcc and the merge will be handled by the
                # initial vertex merge.
                index = self.initial_regset.reg_nodes.index(u)
                self.previous_regset.reg_nodes[index] = prev_result["saved_pcc"]
            else:
                # normal vertex, there is no such thing as an initial
                # epcc that is not a dummy vertex?
                raise SubgraphMergeError(
                    "PccFixup initial epcc is not a dummy vertex")

    def _merge_syscall(self, u):
        """
        Merge a vertex that has been marked as eret capability return
        by the Syscall subparser. This happens when the trace is split
        in the middle of a system call or exception.
        If the previous subparser marked the beginning of an exception,
        the the return value is recorded.
        """
        prev_syscall = self.context.prev_syscall
        curr_syscall = self.context.curr_syscall
        if not prev_syscall or not prev_syscall["active"]:
            return
        if prev_syscall["pc_eret"] == curr_syscall["eret_addr"]:
            u_data = self.subgraph.vp.data[u]
            u_data.add_use_syscall(curr_syscall["eret_time"],
                                   prev_syscall["code"], False)

    def examine_vertex(self, u):
        """
        Merge each vertex of the subgraph in the main merged graph.

        There are 3 cases:
        1. u is a dummy vertex (origin = PARTIAL) in the subgraph,
        therefore it is in the initial regset.
        The corresponding previous regset entry is None.
        2. same as (1) but the corresponding regset entry is not None.
        3. u is a normal vertex (all origin types except PARTIAL).

        Case (1) has 2 sub-cases:
        1.1. all the out-neighbours of u are ROOT vertices.
        In this case the dummy vertex u is deleted and the out-neightbours
        are moved to the merged-graph.
        1.2. there is at least 1 out-neighbour of u that is not a ROOT.
        In this case a MissingParentError is raised, there is nothing to
        derive a non-root vertex from.

        Case (2) has 2 sub-cases:
        2.1. all the out-neighbours of u are ROOT vertices.
        The ROOT vertices are not moved to the merged-graph, instead
        their out-neightbours are attached to the existing parent from
        the previous regset. This is because the ROOTs must not be created
        if we have something to derive from.
        2.2. there is at least 1 out-neightbour of u that is not a ROOT.
        In this case ROOT vertices are suppressed as in (2.1) and the
        non-ROOT vertices are directly attached to the corresponding vertex
        in the previous regset (in the merged-graph).

        Case (3) is trivial to handle, the vertex is moved to the merged
        graph and the edges are recreated.
        """
        if self.omit_vertex_map[u]:
            # nothing to do for this vertex, it is marked to be omitted
            return

        if self.context.step_idx == 0:
            # merge initial and normal vertices but ignore the vertex memory map
            if u in self.initial_regset.reg_nodes or u == self.initial_regset.pcc:
                logger.debug("Merge initial vertex subgraph:%s", u)
                self._merge_initial_vertex(u)
            else:
                logger.debug("Merge normal vertex subgraph:%s", u)
                self._merge_subgraph_vertex(u)
        else:
            # handle syscall merges
            if u == self.context.curr_pcc_fixup["epcc"]:
                self._merge_pcc_fixup(u)
            if u == self.context.curr_syscall["eret_cap"]:
                self._merge_syscall(u)
            # merge vertices
            if u in self.initial_regset.reg_nodes or u == self.initial_regset.pcc:
                logger.debug("Merge initial vertex subgraph:%s", u)
                self._merge_initial_vertex(u)
            elif u in self.vertex_map.initial_map.values():
                logger.debug("Merge initial mem vertex subgraph:%s", u)
                self._merge_initial_mem_vertex(u)
            else:
                logger.debug("Merge normal vertex subgraph:%s", u)
                self._merge_subgraph_vertex(u)


class CapabilityBranchSubparser:
    """
    Handle capability branch instructions.
    Subparser that fixes the content of pcc/epcc
    when a capability branch with an exception is
    found.
    """

    def __init__(self, parser):
        self.parser = parser

        self._saved_pcc = None
        """Saved PCC vertex handle before a cj[al]r with an exception."""
        self._saved_addr = None
        """Address of the last cj[al]r with exception seen."""

        self._save_first_mfc = True
        """
        Flag used to determine whether the initial state
        should be saved.
        """

        self._initial_epcc = None
        """Epcc found at the initial mfc0."""

        self._initial_badvaddr = None
        """First badvaddr fetched for which we did not see the exception."""

        self._saved_epcc_out_neighbours = None
        """
        Saved out neighbours of the jmp target so that we can
        detect anything appended to it.
        """

    @property
    def pgm(self):
        return self.parser.pgm

    @property
    def regset(self):
        return self.parser.regset

    def mp_result(self):
        """
        Return partial result from worker subparser
        """
        # serialize vertex index, not object
        try:
            saved_pcc = int(self._saved_pcc)
        except TypeError:
            saved_pcc = None
        try:
            epcc_neighbours = [int(u) for u in self._saved_epcc_out_neighbours]
        except TypeError:
            epcc_neighbours = None

        try:
            epcc = int(self._initial_epcc)
        except TypeError:
            epcc = None

        state = {
            "saved_addr": self._saved_addr,
            "saved_pcc": saved_pcc,
            "epcc_out_neighbours": epcc_neighbours,
            "epcc": epcc,
            "badvaddr": self._initial_badvaddr,
        }
        return state

    def scan_dmfc0(self, inst, entry, regs, last_regs, idx):
        """
        When badvaddr is loaded, capture its value and make
        a decision about what has been stored in epcc
        if before there was an exception involving
        a capability branch.
        """
        if self._saved_addr != None:
            self._save_first_mfc = False
            # badvaddr
            if inst.op1.gpr_index == 8:
                badvaddr = inst.op0.value
                if (badvaddr == self._saved_addr or
                    badvaddr == self._saved_addr + 4):
                    # not committed, epcc = pcc_before_jmp
                    # XXX this assumes that nothing as been done with epcc
                    # between the exception and the mfc0 instruction
                    assert (self.regset[31].out_degree() ==
                            len(self._saved_epcc_out_neighbours))
                    self.regset[31] = self._saved_pcc
            self._saved_addr = None
        elif self._save_first_mfc and inst.op1.gpr_index == 8:
            self._save_first_mfc = False
            self._initial_badvaddr = inst.op0.value
            self._initial_epcc = self.regset[31]
        return False

    def scan_eret(self, inst, entry, regs, last_regs, idx):
        self._save_first_mfc = False
        return False

    def _save_branch_state(self, entry, branch_target):
        """
        Save the state when a capability branch with
        an exception is found.
        """
        self._save_first_mfc = False
        self._saved_pcc = self.regset.pcc
        self._saved_addr = entry.pc
        self._saved_epcc_out_neighbours = list(branch_target.out_neighbours())

    def scan_cjr(self, inst, entry, regs, last_regs, idx):
        """
        Discard current pcc and replace it.
        If the cjr has an exception, the previous pcc is saved
        so that if the instruction did not commit, epcc can
        be set to the correct pcc.
        """
        # discard current pcc and replace it
        if self.regset.has_reg(inst.op0.cap_index):
            # we already have a node for the new PCC
            new_pcc = self.regset[inst.op0.cap_index]
            if inst.has_exception:
                self._save_branch_state(entry, new_pcc)
            self.regset.pcc = new_pcc
            pcc_data = self.pgm.data[self.regset.pcc]
            if not pcc_data.cap.has_perm(CheriCapPerm.EXEC):
                logger.error("Loading PCC without exec permissions? %s %s",
                             inst, pcc_data)
                raise UnexpectedOperationError(
                    "Loading PCC without exec permissions")
        else:
            # we should create a node here but this should really
            # not be happening, the node is None only when the
            # register content has never been seen before.
            logger.error("Found cjr with unexpected "
                         "target capability %s", inst)
            raise UnexpectedOperationError("cjr to unknown capability")
        return False

    def scan_cjalr(self, inst, entry, regs, last_regs, idx):
        # save current pcc
        cd_idx = inst.op0.cap_index
        if not self.regset.has_pcc(allow_root=True):
            # create a root node for PCC that is in cd
            old_pcc_node = self.parser.make_root_node(entry, inst.op0.value,
                                                      time=entry.cycles)
        else:
            old_pcc_node = self.regset.pcc
        self.regset[cd_idx] = old_pcc_node

        # discard current pcc and replace it
        if self.regset.has_reg(inst.op1.cap_index):
            # we already have a node for the new PCC
            new_pcc = self.regset[inst.op1.cap_index]
            if inst.has_exception:
                self._save_branch_state(entry, new_pcc)
            self.regset.pcc = new_pcc
            pcc_data = self.pgm.data[self.regset.pcc]
            if not pcc_data.cap.has_perm(CheriCapPerm.EXEC):
                logger.error("Loading PCC without exec permissions? %s %s",
                             inst, pcc_data)
                raise UnexpectedOperationError(
                    "Loading PCC without exec permissions")
        else:
            # we should create a node here but this should really
            # not be happening, the node is None only when the
            # register content has never been seen before.
            logger.error("Found cjalr with unexpected "
                         "target capability %s", inst)
            raise UnexpectedOperationError("cjalr to unknown capability")
        return False

    def scan_ccall(self, inst, entry, regs, last_regs, idx):
        # XXX TODO the semantic regarding ccall
        # depends on the selector field, we may not
        # have an exception here, or always have one
        raise NotImplementedError("ccall pcc fixup not yet implemented")

    def scan_creturn(self, inst, entry, regs, last_regs, idx):
        # XXX TODO the semantic regarding ccall
        # depends on the selector field, we may not
        # have an exception here, or always have one
        raise NotImplementedError("creturn pcc fixup not yet implemented")


class SyscallSubparser:
    """
    Handle the system call vertex generation.

    This subparser groups the callbacks that keep the
    exception state
    This class contains all the methods that manipulate
    registers and values that depend on the ABI and constants
    in CheriBSD.

    XXX: multiprocessing not yet supported.
    Merging the syscall state is not straightforward: we do not
    know the correct eret, so we may look for the first eret that
    returns to userspace, although if we do not generate vertices it
    may be easier to merge.
    """

    SYS_RET = -1

    syscall_codes = {
        447: ("mmap", SYS_RET),
        228: ("shmat", SYS_RET),
        73: ("munmap", 3), # arg in c3
        230: ("shmdt", 3), # arg in c3
    }
    """
    Syscall fetching configuration. This defines the
    syscall codes we care about and which arguments/return values
    we should record.
    The format of the map is the following:
    syscall_code =>  (syscall_name, register_number)
    """

    def __init__(self, parser):
        self.parser = parser

        self.in_syscall = False
        """Flag indicates whether we are tracking a systemcall."""

        self.pc_eret = None
        """Expected eret instruction PC."""

        self.code = None
        """Current syscall code."""

        self.exception_depth = 0
        """Number of nested exceptions"""

        self.initial_eret_cap = None
        """
        Capability returned by first eret not matched by any preceding
        syscall/exception.
        """

        self.initial_eret_addr = None
        """
        Return address of the first eret not matched by any preceding
        syscall/exception.
        """

        self.initial_eret_time = None
        """
        Time of the first eret not matched by any preceding
        syscall/exception.
        """

    @property
    def pgm(self):
        return self.parser.pgm

    @property
    def regset(self):
        return self.parser.regset

    def mp_result(self):
        try:
            eret_cap_idx = int(self.initial_eret_cap)
        except TypeError:
            eret_cap_idx = None
        result = {
            "code": self.code,
            "active": self.in_syscall,
            "pc_eret": self.pc_eret,
            "eret_time": self.initial_eret_time,
            "eret_cap": eret_cap_idx,
            "eret_addr": self.initial_eret_addr,
        }
        return result

    def _get_syscall_code(self, regs):
        """Get the syscall code for direct and indirect syscalls."""
        # syscall code in $v0
        # syscall arguments in $a0-$a7/$c3-$c10
        code = regs.gpr[1] # $v0
        indirect_code = regs.gpr[3] # $a0
        is_indirect = (code == 0 or code == 198)
        return indirect_code if is_indirect else code

    def scan_exception(self, inst, entry, regs, last_regs, idx):
        """
        When an exception occurs, adjust the epcc vertex from pcc.
        """
        self.exception_depth += 1
        logger.debug("except {%d}: update epcc %s, update pcc %s",
                     entry.cycles,
                     self.pgm.data[self.regset.pcc],
                     self.pgm.data[self.regset[29]])
        self.regset[31] = self.regset.pcc # saved pcc
        self.regset.pcc = self.regset[29] # pcc <- kcc
        return False

    def scan_syscall(self, inst, entry, regs, last_regs, idx):
        """
        Scan a syscall instruction and detect the syscall type
        and arguments.
        """
        self.code = self._get_syscall_code(regs)
        try:
            record = SyscallSubparser.syscall_codes[self.code]
            if record[1] != SyscallSubparser.SYS_RET:
                # record the use of a vertex as system call argument
                vertex = self.regset[record[1]]
                data = self.pgm.data[vertex]
                logger.debug("Detected syscall %d capability argument: %s",
                             self.code, data)
                data.add_use_syscall(entry.cycles, self.code, True)
            else:
                self.in_syscall = True
                self.pc_eret = entry.pc + 4
        except KeyError:
            # not interested in the syscall
            pass
        return False

    def scan_eret(self, inst, entry, regs, last_regs, idx):
        """
        Scan eret instructions to properly restore pcc from epcc
        and capture syscall return values.
        """
        self.exception_depth -= 1
        epcc_valid = regs.valid_caps[31]
        if not epcc_valid:
            msg = "eret without valid epcc register"
            logger.error(msg)
            raise UnexpectedOperationError(msg)
        epcc = regs.cap_reg[31]
        if self.exception_depth < 0:
            # the trace begins within a syscall/exception
            self.initial_eret_cap = self.regset[3]
            self.initial_eret_addr = epcc.base + epcc.offset
            self.initial_eret_time = entry.cycles
            # restore a 0 exception depth
            self.exception_depth = 0

        if (self.in_syscall and
            epcc.base + epcc.offset == self.pc_eret):
            self.in_syscall = False
            vertex = self.regset[3]
            data = self.pgm.data[vertex]
            logger.debug("Detected syscall %d capability return: %s",
                         self.code, data)
            data.add_use_syscall(entry.cycles, self.code, False)
        self.regset.pcc = self.regset[31] # restore saved pcc
        return False


class InitialStackAccessSubparser:
    """
    Detect the location and size of the initial user stack.
    The initial stack location is then set as a graph property
    on the merged graph. This information can be used later
    in the processing.

    Note: this subparser is only attached to the first worker
    because we do not care about it in the rest of the trace.

    XXX we may extend this if we actually have to detect multiple
    processes being spawned. This requires a different level of
    abstraction in the graph anyway.
    """

    def __init__(self, parser):
        self.parser = parser

        self.first_eret = False
        """First eret seen, userspace started."""

    def scan_eret(self, inst, entry, regs, last_regs, idx):
        if self.first_eret:
            return False
        self.first_eret = True
        stack_valid = regs.valid_caps[11]
        sp_valid = regs.valid_gprs[29]
        if not stack_valid or not sp_valid:
            logger.warning("Invalid stack capability or stack pointer "
                           "at return to userspace")
        # remember the stack base and bound to look for accesses in that range
        stack_cap = regs.cap_reg[11]
        self.parser.pgm.stack = CheriCap(stack_cap)
        self.parser.pgm.stack.offset = regs.gpr[29]
        return False


class PointerProvenanceParser(MultiprocessCallbackParser):
    """
    Parsing logic that builds the provenance graph used in
    all the provenance-based plots.
    """

    def __init__(self, cache=False, **kwargs):
        super().__init__(**kwargs)

        self.cache = cache
        """Are we using a cached dataset."""

        self.pgm = None
        """Provenance graph manager, proxy access to the provenance graph."""

        self._init_graph()

        self.regset = RegisterSet(self.pgm)
        """
        Register set that maps capability registers
        to nodes in the provenance tree.
        """

        self.vertex_map = VertexMemoryMap(self.pgm)
        """
        Helper that tracks the graph vertex stored at
        a given memory location.
        Internally also keeps track of the vertices that are
        stored/loaded in previously unseen memory addresses.
        This is used to correctly merge the subgraphs from
        multiprocessing workers.
        """

        self.initial_regset = None
        """
        The initial register set is created in worker processes
        to keep track of the initial dummy graph vertices that
        are created. This is used to correctly merge the
        subgraphs.
        """

        self._cbk_names = (["cjr", "cjalr", "ccall", "creturn", "cfromptr",
                            "csetbounds", "csetboundsexact", "candperm"] +
                           self._cbk_manager.iclass_map[IClass.I_CAP_CPREG])
        """
        Names of callbacks for capability instructions that have custom
        register-set handling (the set also includdes IClass.I_CAP_STORE
        and IClass.I_CAP_LOAD but it is easier to check for trace_entry
        is_load and is_store). This is required to properly update the
        register set when other capability instructions are found and
        is placed here to avoid rebuilding the list at every callback
        invocation.
        """

        self._initial_stack = InitialStackAccessSubparser(self)
        self._pcc_fixup = CapabilityBranchSubparser(self)
        self._syscall_subparser = SyscallSubparser(self)
        self._add_subparser(self._initial_stack)
        self._add_subparser(self._syscall_subparser)
        self._add_subparser(self._pcc_fixup)


    def _init_graph(self):
        if self.cache:
            cache_file = self.path + "_provenance.gt"
            self.pgm = ProvenanceGraphManager(cache_file)
        else:
            self.pgm = ProvenanceGraphManager()

    def parse(self, *args, **kwargs):
        with ProgressTimer("Parse provenance graph", logger):
            if self.cache and not self.pgm.cache_exists:
                super().parse(*args, **kwargs)
                self.pgm.save()
            elif not self.cache:
                super().parse(*args, **kwargs)

    def get_model(self):
        return self.pgm

    def mp_result(self):
        """
        Return the partial result from a worker process.

        The returned data is a tuple containing:
        - the partial graph
        - the initial register set if this worker did not
          parse the first chunk of the trace
        - the final register set
        - the initial and final vertex memory maps,
        holding the live vertices in memory

        :return: dict(partial_graph, initial_regset, final_regset,
        vertex_map)
        """
        state = {
            "pgm": self.get_model(),
            "initial_regset": self.initial_regset,
            "final_regset": self.regset,
            "mem_vertex_map": self.vertex_map,
            "sub_pcc_fixup": self._pcc_fixup.mp_result(),
            "sub_syscall": self._syscall_subparser.mp_result(),
        }
        return state

    def mp_merge(self, results):
        """
        Populate the dataset from the partial results.

        Note: this method is run in the main process,
        assuming that the results are in-order w.r.t.
        the trace entries indexes that were used.
        """
        if self.mp.threads == 1:
            # need to merge partial vertices from the beginning of
            # the trace anyway, reinit the graph manager with an
            # empty one, the previous is in the results list
            # XXX this is potentially wasteful for the 1-thread case
            self._init_graph()
        merge_ctx = MergePartialSubgraphContext(self.pgm)
        for idx, result in enumerate(results):
            with ProgressTimer("Merge partial worker result [%d/%d]" % (
                    idx + 1, len(results)), logger):
                merge_ctx.step(result)

    def _do_parse(self, start, end, direction):
        """
        This sets up the different initialization of the graph and
        register set, depending on the start index.

        If the start == 0 then we initialize the register set from
        what we find before the first return to userspace.
        Otherwise we create dummy graph roots for each register
        that will be used during the merge.
        """
        self.initial_regset = RegisterSet(self.pgm)
        # create dummy initial nodes
        reg_nodes = list(self.pgm.graph.add_vertex(32))
        pcc_node = self.pgm.graph.add_vertex()
        for n in reg_nodes + [pcc_node]:
            data = NodeData()
            data.origin = CheriNodeOrigin.PARTIAL
            self.pgm.data[n] = data
        self.initial_regset.reg_nodes = reg_nodes
        self.initial_regset.pcc = pcc_node
        self.regset.reg_nodes = list(reg_nodes)
        self.regset.pcc = pcc_node
        # use the MP vertex map
        self.vertex_map = MPVertexMemoryMap(self.pgm)
        super()._do_parse(start, end, direction)
        self.pgm.save("initial_stack_test.gt")

    def _has_exception(self, entry, code=None):
        """
        Check if an exception occurred in the given trace entry
        """
        if code is not None:
            return entry.exception == code
        else:
            return entry.exception != 31

    def scan_cclearregs(self, inst, entry, regs, last_regs, idx):
        """
        Clear the register set according to the mask.
        The result can not be immediately found in the trace, it
        is otherwise spread among all the uses of the registers.
        """
        raise NotImplementedError("cclearregs not yet supported")
        return False

    def _handle_cpreg_get(self, regnum, inst, entry):
        """
        When a cgetXXX is found, propagate the node from the special
        register XXX (i.e. kcc, kdc, ...) to the destination or create a
        new node if nothing was there.

        :param regnum: the index of the special register in the register set
        :type regnum: int

        :param inst: parsed instruction
        :type inst: :class:`cheriplot.core.parser.Instruction`

        :parm entry: trace entry
        :type entry: :class:`pycheritrace.trace_entry`
        """
        if not self.regset.has_reg(regnum, allow_root=True):
            # no node was ever created for the register, it contained something
            # invalid
            node = self.make_root_node(entry, inst.op0.value,
                                       time=entry.cycles)
            self.regset[regnum] = node
            logger.debug("cpreg_get: new node from $c%d %s",
                         regnum, self.pgm.data[node])
        self.regset[inst.op0.cap_index] = self.regset[regnum]

    def _handle_cpreg_set(self, regnum, inst, entry):
        """
        When a csetXXX is found, propagate the node to the special
        register XXX (i.e. kcc, kdc, ...) or create a new node.

        :param regnum: the index of the special register in the register set
        :type regnum: int

        :param inst: parsed instruction
        :type inst: :class:`cheriplot.core.parser.Instruction`

        :parm entry: trace entry
        :type entry: :class:`pycheritrace.trace_entry`
        """
        # XXX should write a test case for this
        if not self.regset.has_reg(inst.op1.cap_index, allow_root=True):
            node = self.make_root_node(entry, inst.op0.value,
                                       time=entry.cycles)
            self.regset[inst.op1.cap_index] = node
            logger.debug("cpreg_set: new node from c<%d> %s",
                         regnum, self.pgm.data[node])
        self.regset[regnum] = self.regset[inst.op1.cap_index]

    def scan_cgetepcc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_get(31, inst, entry)
        return False

    def scan_csetepcc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_set(31, inst, entry)
        return False

    def scan_cgetkcc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_get(29, inst, entry)
        return False

    def scan_csetkcc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_set(29, inst, entry)
        return False

    def scan_cgetkdc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_get(30, inst, entry)
        return False

    def scan_csetkdc(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_set(30, inst, entry)
        return False

    def scan_cgetdefault(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_get(0, inst, entry)
        return False

    def scan_csetdefault(self, inst, entry, regs, last_regs, idx):
        self._handle_cpreg_set(0, inst, entry)
        return False

    def scan_cgetpcc(self, inst, entry, regs, last_regs, idx):
        if not self.regset.has_pcc(allow_root=True):
            # never seen anything in pcc so we create a new node
            node = self.make_root_node(entry, inst.op0.value,
                                       time=entry.cycles)
            self.regset.pcc = node
            logger.debug("cgetpcc: new node from pcc %s",
                         self.pgm.data[node])
        self.regset[inst.op0.cap_index] = self.regset.pcc
        return False

    def scan_cgetpccsetoffset(self, inst, entry, regs, last_regs, idx):
        return self.scan_cgetpcc(inst, entry, regs, last_regs, idx)

    def scan_csetbounds(self, inst, entry, regs, last_regs, idx):
        """
        Each csetbounds is a new pointer allocation
        and is recorded as a new node in the provenance tree.
        The destination register is associated to the new node
        in the register set.

        csetbounds:
        Operand 0 is the register with the new node
        Operand 1 is the register with the parent node
        """
        node = self.make_node(entry, inst, origin=CheriNodeOrigin.SETBOUNDS)
        self.regset[inst.op0.cap_index] = node
        return False

    def scan_cfromptr(self, inst, entry, regs, last_regs, idx):
        """
        Each cfromptr is a new pointer allocation and is
        recodred as a new node in the provenance tree.
        The destination register is associated to the new node
        in the register set.

        cfromptr:
        Operand 0 is the register with the new node
        Operand 1 is the register with the parent node
        """
        node = self.make_node(entry, inst, origin=CheriNodeOrigin.FROMPTR)
        self.regset[inst.op0.cap_index] = node
        return False

    def scan_candperm(self, inst, entry, regs, last_regs, idx):
        """
        Each candperm is a new pointer allocation and is recorded
        as a new node in the provenance tree.

        candperm:
        Operand 0 is the register with the new node
        Operand 1 is the register with the parent node
        """
        node = self.make_node(entry, inst, origin=CheriNodeOrigin.ANDPERM)
        self.regset[inst.op0.cap_index] = node
        return False

    def scan_cap(self, inst, entry, regs, last_regs, idx):
        """
        Whenever a capability instruction is found, update
        the mapping from capability register to the provenance
        tree node associated to the capability in it.
        """
        if entry.is_store or entry.is_load or inst.opcode in self._cbk_names:
            return False
        else:
            self.update_regs(inst, entry, regs, last_regs, idx)
        return False

    def _handle_dereference(self, inst, entry, ptr_reg):
        """
        Store offset at time of dereference of a given capability.
        """
        try:
            node = self.regset[ptr_reg]
        except KeyError:
            logger.error("{%d} Dereference unknown capability %s",
                         entry.cycles, inst)
            raise DereferenceUnknownCapabilityError(
                "Dereference unknown capability")
        if node is None:
            logger.error("{%d} Dereference unknown capability %s",
                         entry.cycles, inst)
            raise DereferenceUnknownCapabilityError(
                "Dereference unknown capability")
        node_data = self.pgm.data[node]
        # instead of the capability register offset we use the
        # entry memory_address so we capture any extra offset in
        # the instruction as well
        is_cap = inst.opcode.startswith("clc") or inst.opcode.startswith("csc")
        if entry.is_load:
            node_data.add_deref_load(entry.cycles, entry.memory_address,
                                     is_cap)
        elif entry.is_store:
            node_data.add_deref_store(entry.cycles, entry.memory_address,
                                      is_cap)
        else:
            if not self._has_exception(entry):
                logger.error("Dereference is neither a load or a store %s", inst)
                raise RuntimeError("Dereference is neither a load nor a store")

    def scan_cap_load(self, inst, entry, regs, last_regs, idx):
        """
        Store all offsets at time of dereference of a given capability.

        clX[u] have pointer argument in op3
        clXr and clXi have pointer argument in op2
        cllX have pointer argument in op1
        """
        # get the register with the address capability
        # this may be a normal capability load or a linked-load
        if inst.opcode.startswith("cll"):
            ptr_reg = inst.op1.cap_index
        else:
            if inst.opcode[-1] == "r" or inst.opcode[-1] == "i":
                ptr_reg = inst.op2.cap_index
            else:
                ptr_reg = inst.op3.cap_index
        self._handle_dereference(inst, entry, ptr_reg)
        return False

    def scan_cap_store(self, inst, entry, regs, last_regs, idx):
        """
        Store all offsets at time of dereference of a given capability.

        csX have pointer argument in op3
        csXr and csXi have pointer argument in op2
        cscX conditionals use op2
        """
        # get the register with the address capability
        # this may be a normal capability store or an atomic-store
        if inst.opcode != "csc" and inst.opcode.startswith("csc"):
            # atomic
            ptr_reg = inst.op2.cap_index
        else:
            if inst.opcode[-1] == "r" or inst.opcode[-1] == "i":
                ptr_reg = inst.op2.cap_index
            else:
                ptr_reg = inst.op3.cap_index
        self._handle_dereference(inst, entry, ptr_reg)
        return False

    def scan_clc(self, inst, entry, regs, last_regs, idx):
        """
        clc:
        Operand 0 is the register with the new node
        The parent is looked up in memory or a root node is created
        """
        cd = inst.op0.cap_index
        node = self.vertex_map.mem_load(entry.memory_address)
        if node is None:
            logger.debug("Load c%d from new location 0x%x",
                         cd, entry.memory_address)
        # if the capability loaded from memory is valid, it
        # can be safely assumed that it corresponds to the node
        # stored in the memory_map for that location, if there is
        # one. If there is no node in the memory_map then a
        # new node can be created from the valid capability.
        # Otherwise something has changed the memory location so we
        # clear the memory_map and the regset entry.
        if not inst.op0.value.valid:
            logger.debug("clc load invalid, clear memory vertex map")
            self.regset[cd] = None
            if node is not None:
                self.vertex_map.clear(entry.memory_address)
        else:
            # check if the load instruction has committed
            old_cd = CheriCap(last_regs.cap_reg[cd])
            curr_cd = CheriCap(regs.cap_reg[cd])
            logger.debug("clc op0 valid old_cd %s curr_cd %s", old_cd, curr_cd)
            if old_cd != curr_cd or not self._has_exception(entry):
                # the destination register was updated so the
                # instruction did commit

                if node is None:
                    # add a node as a root node because we have never
                    # seen the content of this register yet.
                    node = self.make_root_node(entry, inst.op0.value,
                                               time=entry.cycles)
                    node_data = self.pgm.data[node]
                    logger.debug("Found %s value %s from memory load",
                                 inst.op0.name, node_data)
                    self.vertex_map.mem_load(entry.memory_address, node)
                node_data = self.pgm.data[node]
                node_data.add_mem_load(entry.cycles, entry.memory_address)
                self.regset[cd] = node
        return False

    scan_clcr = scan_clc
    scan_clci = scan_clc

    def scan_csc(self, inst, entry, regs, last_regs, idx):
        """
        Record the locations where a capability node is stored.
        This is later used if the capability is loaded again with
        a clc.
        The locations where a capability is stored are also saved in
        the graph.
        It may happen that a previously unseen register is stored,
        the value of the register is now known to be valid because it
        is stored in the trace entry, a root node is created.

        csc:
        Operand 0 is the capability being stored, the node already exists
        """
        cd = inst.op0.cap_index

        if inst.op0.value.valid:
            # if this is not a data access

            if not self.regset.has_reg(cd, allow_root=True):
                # XXX may decide to disable and have an exception here
                # need to create one
                node = self.make_root_node(entry, inst.op0.value,
                                           time=entry.cycles)
                self.regset[cd] = node
                logger.debug("Found %s value %s from memory store",
                             inst.op0.name, node)
            else:
                node = self.regset[cd]

            # if there is a node associated with the register that is
            # being stored, save it in the memory_map for the memory location
            # written by csc
            self.vertex_map.mem_store(entry.memory_address, node)
            # set the address attribute of the node vertex data property
            node_data = self.pgm.data[node]
            node_data.add_mem_store(entry.cycles, entry.memory_address)

        return False

    scan_cscr = scan_csc
    scan_csci = scan_csc

    def make_root_node(self, entry, cap, time=0, pc=None):
        """
        Create a root node of the provenance graph and add it to the dataset.

        :param entry: trace entry of the current instruction
        :type entry: `pycheritrace.trace_entry`
        :param cap: capability register value
        :type cap: :class:`pycheritrace.capability_register`
        :param time: optional allocation time
        :type time: int
        :param: pc: optional PC value for the root node
        :type pc: int
        :return: the newly created node
        :rtype: :class:`graph_tool.Vertex`
        """
        data = NodeData()
        data.cap = CheriCap(cap)
        # if pc is 0 indicate that we do not have a specific
        # instruction for this
        data.cap.t_alloc = time
        data.pc = entry.pc if pc is None else pc
        data.origin = CheriNodeOrigin.ROOT
        data.is_kernel = entry.is_kernel()

        # create graph vertex and assign the data to it
        vertex = self.pgm.graph.add_vertex()
        self.pgm.data[vertex] = data
        return vertex

    def make_node(self, entry, inst, origin=None, src_op_index=1, dst_op_index=0):
        """
        Create a node in the provenance tree.
        The parent is fetched from the register set depending on the source
        registers of the current instruction.

        :param entry: trace entry info object
        :type entry: :class:`pycheritrace.trace_entry`
        :param inst: instruction parsed
        :type inst: :class:`cheriplot.core.parser.Instruction`
        :param origin: the instruction/construction that originated the node
        :type origin: :class:`cheriplot.core.provenance.CheriNodeOrigin`
        :param src_op_index: index of the instruction operand that
        associated with the parent node
        :type src_op_index: int
        :param dst_op_index: index of the instruction operand with
        the node data
        :type dst_op_index: int
        :return: the new node
        :rtype: :class:`graph_tool.Vertex`
        """
        data = NodeData.from_operand(inst.operands[dst_op_index])
        data.origin = origin
        # try to get a parent node
        op = inst.operands[src_op_index]
        if self.regset.has_reg(op.cap_index, allow_root=False):
            parent = self.regset[op.cap_index]
        else:
            logger.error("Error searching for parent node of %s", data)
            raise MissingParentError("Missing parent for %s" % data)

        # there must be a parent if the root nodes for the initial register
        # set have been created
        # Note that we may chose to add a root node when no parent is
        # available, this may be the case of replacing the guess of KDC
        if parent == None:
            logger.error("Missing parent for %s, src_operand=%d %s, "
                         "dst_operand=%d %s", data,
                         src_op_index, inst.operands[src_op_index],
                         dst_op_index, inst.operands[dst_op_index])
            raise MissingParentError("Missing parent for %s" % data)

        # create the vertex in the graph and assign the data to it
        vertex = self.pgm.graph.add_vertex()
        self.pgm.graph.add_edge(parent, vertex)
        self.pgm.data[vertex] = data
        return vertex

    def update_regs(self, inst, entry, regs, last_regs, idx):
        """
        Try to update the registers-node mapping when a capability
        instruction is executed so that nodes are propagated in
        the registers when their bounds do not change.
        """
        dst = inst.op0
        src = inst.op1

        if dst and dst.is_capability:
            if src and src.is_capability:
                src_vertex = self.regset.has_reg(src.cap_index, allow_root=True)
            else:
                src_vertex = False

            if src_vertex:
                self.regset[dst.cap_index] = self.regset[src.cap_index]
            else:
                last_valid = last_regs.valid_caps[dst.cap_index]
                curr_valid = regs.valid_caps[dst.cap_index]
                if (not last_valid and curr_valid) or idx == 0:
                    # a register that was invalid has become valid, create a
                    # root for it.
                    dst_vertex = self.make_root_node(
                        entry, dst.value, pc=entry.pc, time=entry.cycles)
                    self.regset[src.cap_index] = dst_vertex
                    self.regset[dst.cap_index] = dst_vertex
