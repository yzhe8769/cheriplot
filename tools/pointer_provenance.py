#-
# Copyright (c) 2016 Alfredo Mazzinghi
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

"""
This script produces a poiner provenance plot from a cheri trace file.
"""

import argparse as ap
import sys
import logging
import cProfile
import pstats

from cheriplot.core.tool import PlotTool
from cheriplot.plot.provenance import (
    PointerTreePlot, AddressMapPlot, PointedAddressFrequencyPlot,
    SyscallAddressMapPlot)

logger = logging.getLogger(__name__)

class ProvenancePlotTool(PlotTool):

    description = "Plot pointer provenance from cheri trace"

    def init_arguments(self):
        super(ProvenancePlotTool, self).init_arguments()
        self.parser.add_argument("--tree", help="Draw a provenance tree plot",
                                 action="store_true")
        self.parser.add_argument("--asmap",
                                 help="Draw an address-map plot (default)",
                                 action="store_true")
        self.parser.add_argument("--pfreq",
                                 help="Draw frequency of reference plot",
                                 action="store_true")
        self.parser.add_argument("--syscallmap",
                                 help="Draw address-map plot with only system calls",
                                 action="store_true")
        self.parser.add_argument("-m", "--vmmap-file",
                                 help="CSV file containing the VM map dump"
                                 " generated by procstat")

    def _run(self, args):
        if args.tree:
            plot = PointerTreePlot(args.trace, args.cache)
        elif args.pfreq:
            plot = PointedAddressFrequencyPlot(args.trace, args.cache)
            if args.vmmap_file:
                plot.set_vmmap(args.vmmap_file)
        elif args.syscallmap:
            plot = SyscallAddressMapPlot(args.trace, args.cache)
        else:
            plot = AddressMapPlot(args.trace, args.cache)
            if args.vmmap_file:
                plot.set_vmmap(args.vmmap_file)

        if args.outfile:
            plot.plot_file = args.outfile

        plot.show()

if __name__ == "__main__":
    tool = ProvenancePlotTool()
    tool.run()
