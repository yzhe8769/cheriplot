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

import logging

from cheriplot.core import (
    BaseToolTaskDriver, Argument, Option, NestedConfig, ProgressTimer)
from cheriplot.vmmap import VMMapFileParser
from cheriplot.provenance.model import ProvenanceGraphManager
from cheriplot.provenance.visit import (
    FilterNullVertices, FilterKernelVertices, FilterCfromptr, MergeCfromptr,
    ProvGraphTimeSlice, FilterStackVertices, ChainGraphVisit)

logger = logging.getLogger(__name__)

class GraphFilterDriver(BaseToolTaskDriver):
    """Driver that implements the top-level filtering tool."""
    description = """
    Graph filtering tool.
    This tool processes a cheriplot graph to produce a filtered version.
    The filtered graph still includes all the vertices but carries a mask
    that removes some of the vertices when used.
    """

    graph = Argument(help="Path to the cheriplot graph")
    outfile = Option(
        default=None,
        help="Path to the output file")
    display_name = Option(
        default=None,
        help="New display-name for the graph")
    purge = Option(
        action="store_true",
        help="Purge filtered elements in the output graph. "
        "This is not reversible.")
    incremental = Option(
        action="store_true",
        help="Do not remove existing graph filters.")
    vmmap = NestedConfig(VMMapFileParser)
    no_null = Option(
        action="store_true",
        help="Filter null vertices")
    no_kernel = Option(
        action="store_true",
        help="Filter kernel vertices")
    no_cfromptr = Option(
        action="store_true",
        help="Filter cfromptr vertices")
    no_stack = Option(
        action="store_true",
        help="Filter vertices pointing to the stack")
    aggregate_ptrbounds = Option(
        action="store_true",
        help="Merge sequences of cfromptr+csetbounds. This is not reversible.")
    tslice = Option(
        action="store_true",
        help="Filter a graph slice (see tslice parameters)")
    tslice_mode = Option(
        nargs="+",
        choices=("deref", "create", "access"),
        default=["create"],
        help="""tslice filter mode parameter:
        deref: cap dereference time (load/store/call via capability)
        create: cap create time
        access: cap access time (load/store of the capability)
        """
    )
    tslice_time = Option(
        nargs=2,
        type=int,
        metavar=("start", "end"),
        help="tslice filter start-time and end-time parameters")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.pgm = ProvenanceGraphManager.load(self.config.graph)
        """Loaded graph managers."""

        self._vmmap_parser = VMMapFileParser(config=self.config.vmmap)
        """Process memory mapping CSV parser."""

        self._outfile = self.config.outfile or self.config.graph
        """Output file path, defaults to the input file."""

    def _get_filter(self, pgm):
        """Get a combined filter for a given graph manager."""
        filters = ChainGraphVisit(pgm)
        if self.config.no_null:
            filters += FilterNullVertices(pgm)
        if self.config.no_kernel:
            filters += FilterKernelVertices(pgm)
        if self.config.aggregate_ptrbounds:
             filters+= MergeCfromptr(pgm)
        if self.config.no_cfromptr:
            filters += FilterCfromptr(pgm)
        if self.config.no_stack:
            vmmap = self._vmmap_parser.get_model()
            for entry in vmmap:
                if entry.grows_down:
                    break
            else:
                logger.error("no-stack filter requires vmmap argument")
                raise RuntimeError("np-stack filter requires vmmap argument")
            filters += FilterStackVertices(pgm, entry.start, entry.end)
        if self.config.tslice:
            start, end = self.config.tslice_time
            deref = "deref" in self.config.tslice_mode
            create = "create" in self.config.tslice_mode
            access = "access" in self.config.tslice_mode
            filters += ProvGraphTimeSlice(
                pgm, start, end, creation_time=create,
                deref_time=deref, access_time=access)
        return filters

    def run(self):
        self._vmmap_parser.parse()
        vmmap = self._vmmap_parser.get_model()
        if not self.config.incremental:
            self.pgm.graph.clear_filters()
        graph_filter = self._get_filter(self.pgm)
        filtered_graph = graph_filter(self.pgm.graph)
        vfilt, _ = filtered_graph.get_vertex_filter()
        self.pgm.graph.set_vertex_filter(vfilt)
        if self.config.purge:
            with ProgressTimer("Purge filtered vertices", logger):
                self.pgm.graph.purge_vertices()
        with ProgressTimer("Write output graph", logger):
            self.pgm.save(self._outfile, self.config.display_name)
        
