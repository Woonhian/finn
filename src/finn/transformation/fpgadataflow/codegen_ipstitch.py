import os
import subprocess
import tempfile as tmp

from finn.core.utils import get_by_name
from finn.transformation import Transformation


class CodeGen_ipstitch(Transformation):
    """Create a Vivado IP Block Design project from all the generated IPs of a
    graph. All nodes in the graph must have the fpgadataflow backend attribute,
    and the CodeGen_ipgen transformation must have been previously run on
    the graph. The resulting block design is also packaged as IP.

    Outcome if successful: sets the vivado_stitch_proj attribute in the ONNX
    ModelProto's metadata_props field, with the created project dir as the
    value. A make_project.tcl script is also placed under the same folder,
    which is called to instantiate the per-layer IPs and stitch them together.
    The packaged block design IP can be found under the ip subdirectory.
    """

    def __init__(self, fpgapart):
        super().__init__()
        self.fpgapart = fpgapart

    def apply(self, model):
        ip_dirs = ["list"]
        create_cmds = []
        connect_cmds = []
        # ensure that all nodes are fpgadataflow, and that IPs are generated
        for node in model.graph.node:
            assert node.domain == "finn"
            backend_attribute = get_by_name(node.attribute, "backend")
            assert backend_attribute is not None
            backend_value = backend_attribute.s.decode("UTF-8")
            assert backend_value == "fpgadataflow"
            ip_dir_attribute = get_by_name(node.attribute, "ipgen_path")
            assert ip_dir_attribute is not None
            ip_dir_value = ip_dir_attribute.s.decode("UTF-8")
            ip_dir_value += "/sol1/impl/ip"
            assert os.path.isdir(ip_dir_value)
            ip_dirs += [ip_dir_value]
            vlnv = "xilinx.com:hls:%s:1.0" % node.name
            inst_name = node.name
            create_cmd = "create_bd_cell -type ip -vlnv %s %s" % (vlnv, inst_name)
            create_cmds += [create_cmd]
            # TODO nonlinear topologies: check this for all inputs
            my_producer = model.find_producer(node.input[0])
            if my_producer is None:
                # first node in graph
                # make clock and reset external
                connect_cmds.append(
                    "make_bd_pins_external [get_bd_pins %s/ap_clk]" % inst_name
                )
                connect_cmds.append(
                    "make_bd_pins_external [get_bd_pins %s/ap_rst_n]" % inst_name
                )
                # make input external
                connect_cmds.append(
                    "make_bd_intf_pins_external [get_bd_intf_pins %s/in0_V_V]"
                    % inst_name
                )
            else:
                # intermediate node
                # wire up global clock and reset
                connect_cmds.append(
                    "connect_bd_net [get_bd_ports ap_rst_n_0] [get_bd_pins %s/ap_rst_n]"
                    % inst_name
                )
                connect_cmds.append(
                    "connect_bd_net [get_bd_ports ap_clk_0] [get_bd_pins %s/ap_clk]"
                    % inst_name
                )
                # wire up input to previous output
                # TODO nonlinear topologies: loop over all inputs
                my_in_name = "%s/in0_V_V" % (inst_name)
                prev_out_name = "%s/out_V_V" % (my_producer.name)
                connect_cmds.append(
                    "connect_bd_intf_net [get_bd_intf_pins %s] [get_bd_intf_pins %s]"
                    % (prev_out_name, my_in_name)
                )
            if model.find_consumer(node.output[0]) is None:
                # last node in graph
                # connect prev output to input
                # make output external
                connect_cmds.append(
                    "make_bd_intf_pins_external [get_bd_intf_pins %s/out_V_V]"
                    % inst_name
                )

        # create a temporary folder for the project
        vivado_stitch_proj_dir = tmp.mkdtemp(prefix="vivado_stitch_proj_")
        model.set_metadata_prop("vivado_stitch_proj", vivado_stitch_proj_dir)
        # start building the tcl script
        tcl = []
        # create vivado project
        tcl.append(
            "create_project %s %s -part %s"
            % ("finn_vivado_stitch_proj", vivado_stitch_proj_dir, self.fpgapart)
        )
        # add all the generated IP dirs to ip_repo_paths
        ip_dirs_str = " ".join(ip_dirs)
        tcl.append("set_property ip_repo_paths [%s] [current_project]" % ip_dirs_str)
        tcl.append("update_ip_catalog")
        # create block design and instantiate all layers
        block_name = "finn_design"
        tcl.append('create_bd_design "%s"' % block_name)
        tcl.extend(create_cmds)
        tcl.extend(connect_cmds)
        tcl.append("regenerate_bd_layout")
        tcl.append("validate_bd_design")
        tcl.append("save_bd_design")
        # export block design itself as an IP core
        block_vendor = "xilinx_finn"
        block_library = "finn"
        block_vlnv = "%s:%s:%s:1.0" % (block_vendor, block_library, block_name)
        tcl.append(
            (
                "ipx::package_project -root_dir %s/ip -vendor %s "
                "-library %s -taxonomy /UserIP -module %s -import_files"
            )
            % (vivado_stitch_proj_dir, block_vendor, block_library, block_name)
        )
        tcl.append("set_property core_revision 2 [ipx::find_open_core %s]" % block_vlnv)
        tcl.append("ipx::create_xgui_files [ipx::find_open_core %s]" % block_vlnv)
        tcl.append("ipx::update_checksums [ipx::find_open_core %s]" % block_vlnv)
        tcl.append("ipx::save_core [ipx::find_open_core %s]" % block_vlnv)
        # write the project creator tcl script
        tcl_string = "\n".join(tcl) + "\n"
        with open(vivado_stitch_proj_dir + "/make_project.tcl", "w") as f:
            f.write(tcl_string)
        # create a shell script and call Vivado
        make_project_sh = vivado_stitch_proj_dir + "/make_project.sh"
        working_dir = os.environ["PWD"]
        with open(make_project_sh, "w") as f:
            f.write("#!/bin/bash \n")
            f.write("cd {}\n".format(vivado_stitch_proj_dir))
            f.write("vivado -mode batch -source make_project.tcl\n")
            f.write("cd {}\n".format(working_dir))
        bash_command = ["bash", make_project_sh]
        process_compile = subprocess.Popen(bash_command, stdout=subprocess.PIPE)
        process_compile.communicate()
        return (model, False)