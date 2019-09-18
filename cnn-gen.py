#!/usr/bin/env python3
###################################################################################################
# Copyright (C) 2018-2019 Maxim Integrated Products, Inc. All Rights Reserved.
#
# Maxim Integrated Products, Inc. Default Copyright Notice:
# https://www.maximintegrated.com/en/aboutus/legal/copyrights.html
#
# Written by RM
###################################################################################################
"""
Embedded network and simulation test generator program for Tornado CNN
"""
import hashlib
import os
import signal
import sys

import numpy as np

import apbaccess
import checkpoint
import cmsisnn
import commandline
import kbias
import kernels
import load
import op
import rtlsim
import sampledata
import sampleweight
import stats
import tornadocnn as tc
import yamlcfg
from simulate import cnn1d_layer, cnn2d_layer, linear_layer, passthrough_layer, eltwise_layer
from utils import ffs, fls, popcount


def create_net(prefix,
               verbose,
               debug,
               debug_computation,
               no_error_stop,
               overwrite_ok,
               log,
               apb_base,
               layers,
               operator,
               input_dim,
               pooled_dim,
               output_dim,
               processor_map,
               output_processor_map,
               kernel_size,
               quantization,
               input_chan,
               output_chan,
               output_width,
               padding,
               dilation,
               stride,
               pool,
               pool_stride,
               pool_average,
               activate,
               data,
               kernel,
               bias,
               big_data,
               fc_weights,
               fc_bias,
               split,
               in_offset,
               out_offset,
               streaming,
               flatten,
               input_filename,
               output_filename,
               c_filename,
               base_directory,
               runtest_filename,
               log_filename,
               zero_unused,
               timeout,
               block_mode,
               verify_writes,
               embedded_code=False,
               compact_weights=False,
               compact_data=False,
               write_zero_regs=False,
               weight_filename=None,
               sample_filename=None,
               device=84,
               init_tram=False):
    """
    Chain multiple CNN layers, create and save input and output
    """
    in_expand = [0] * layers
    out_expand = [0] * layers
    in_expand_thresh = [0] * layers
    out_expand_thresh = [0] * layers
    tram_max = [0] * layers

    input_dim_str = [None] * layers
    output_dim_str = [None] * layers
    kernel_size_str = [None] * layers
    pool_str = [None] * layers
    padding_str = [None] * layers
    pool_stride_str = [None] * layers
    stride_str = [None] * layers

    # Check that input channels are in separate memory instances if CHW (big) data format is used,
    # and calculate input and output expansion
    for ll in range(layers):
        if big_data[ll]:
            p = processor_map[ll] >> (ffs(processor_map[ll]) & ~(tc.dev.P_SHARED-1))
            while p:
                if popcount(p & (tc.dev.P_SHARED-1)) > 1:
                    print(f"Layer {ll} uses CHW (big data) input format, but multiple channels "
                          "share the same memory instance. Modify the processor map for "
                          f"layer {ll}.")
                    sys.exit(1)
                p >>= tc.dev.P_SHARED

        out_expand[ll] = (output_chan[ll] + tc.dev.MAX_PROC-1) // tc.dev.MAX_PROC
        out_expand_thresh[ll] = (output_chan[ll] + out_expand[ll]-1) // out_expand[ll]
        if output_chan[ll] > tc.dev.MAX_PROC:
            out_expand_thresh[ll] = \
                (out_expand_thresh[ll] + tc.dev.P_SHARED-1) & ~(tc.dev.P_SHARED-1)
        in_expand[ll] = (input_chan[ll] + tc.dev.MAX_PROC-1) // tc.dev.MAX_PROC
        in_expand_thresh[ll] = (input_chan[ll] + in_expand[ll]-1) // in_expand[ll]
        if input_chan[ll] > tc.dev.MAX_PROC:
            in_expand_thresh[ll] = \
                (in_expand_thresh[ll] + tc.dev.P_SHARED-1) & ~(tc.dev.P_SHARED-1)
        if flatten[ll]:
            in_expand[ll] *= input_dim[ll][0] * input_dim[ll][1]

        # Data memory size check - 4 channels share one instance unless CHW format
        in_size = input_dim[ll][0] * input_dim[ll][1] * in_expand[ll] \
            * (1 if big_data[ll] else 4)
        if in_size + in_offset[ll] > tc.dev.INSTANCE_SIZE*16:
            print(f'Layer {ll}: {1 if big_data[ll] else 4}-channel input size {in_size} '
                  f'with input offset 0x{in_offset[ll]:04x} and expansion {in_expand[ll]}x '
                  f'exceeds data memory instance size of {tc.dev.INSTANCE_SIZE*16}.')
            sys.exit(1)
        out_size = output_dim[ll][0] * output_dim[ll][1] * out_expand[ll] \
            * 4 * output_width[ll] // 8
        if out_size + out_offset[ll] > tc.dev.INSTANCE_SIZE*16:
            print(f'Layer {ll}: 4-channel, {output_width[ll]}-bit output size {out_size} '
                  f'with output offset 0x{out_offset[ll]:04x} and expansion {out_expand[ll]}x '
                  f'exceeds data memory instance size of {tc.dev.INSTANCE_SIZE*16}.')
            sys.exit(1)

        if operator[ll] != op.CONV1D:
            input_dim_str[ll] = f'{input_dim[ll][0]}x{input_dim[ll][1]}'
            output_dim_str[ll] = f'{output_dim[ll][0]}x{output_dim[ll][1]}'
            kernel_size_str[ll] = f'{kernel_size[ll][0]}x{kernel_size[ll][1]}'
            pool_str[ll] = f'{pool[ll][0]}x{pool[ll][1]}' \
                if pool[ll][0] > 1 or pool[ll][1] > 1 else '0x0'
            padding_str[ll] = f'{padding[ll][0]}/{padding[ll][1]}'
            pool_stride_str[ll] = f'{pool_stride[ll][0]}/{pool_stride[ll][1]}'
            stride_str[ll] = f'{stride[ll][0]}/{stride[ll][1]}'
        else:
            input_dim_str[ll] = f'{input_dim[ll][0]}'
            output_dim_str[ll] = f'{output_dim[ll][0]}'
            kernel_size_str[ll] = f'{kernel_size[ll][0]}'
            pool_str[ll] = f'{pool[ll][0]}' \
                if pool[ll][0] > 1 or pool[ll][1] > 1 else '0'
            padding_str[ll] = f'{padding[ll][0]}'
            pool_stride_str[ll] = f'{pool_stride[ll][0]}'
            stride_str[ll] = f'{stride[ll][0]}'

        tram_max[ll] = max(0, pooled_dim[ll][1] + 2*padding[ll][1] - kernel_size[ll][1]) + 1

    # Create comment of the form "k1_b0-1x32x32b_2x2s2p14-..."
    test_name = prefix
    if not embedded_code:
        for ll in range(layers):
            test_name += f'-{input_chan[ll]}x{input_dim_str[ll]}' \
                         f'{"b" if big_data[ll] else "l"}_' \
                         + ("avg" if pool_average[ll]
                            and (pool[ll][0] > 1 or pool[ll][1] > 1) else "") \
                         + ("max" if not pool_average[ll]
                            and (pool[ll][0] > 1 or pool[ll][1] > 1) else "") \
                         + f'{pool_str[ll]}s{pool_stride[ll][0]}' \
                         f'p{padding[ll][0]}' \
                         f'm{output_chan[ll]}' \
                         f'{"_relu" if activate[ll] else ""}'
    MAX_PATH = 255
    if len(test_name) + len(base_directory) > MAX_PATH - 10:
        h = hashlib.md5(test_name.encode()).hexdigest()  # Immutable hash from test name
        cutoff = MAX_PATH - len(test_name) - len(base_directory) - len(h) - 10
        test_name = test_name[:cutoff] + '-' + h
    print(f'{test_name}...')

    os.makedirs(os.path.join(base_directory, test_name), exist_ok=True)

    # Redirect stdout?
    if log:
        sys.stdout = open(os.path.join(base_directory, test_name, log_filename), 'w')
        print(f'{test_name}')

    if block_mode:
        filename = input_filename + '.mem'
    else:
        filename = c_filename + '.c'
    if embedded_code or compact_data:
        sampledata_header = \
            open(os.path.join(base_directory, test_name, sample_filename), mode='w')
    else:
        sampledata_header = None
    if embedded_code or compact_weights:
        weight_header = \
            open(os.path.join(base_directory, test_name, weight_filename), mode='w')
    else:
        weight_header = None

    with open(os.path.join(base_directory, test_name, filename), mode='w') as memfile:
        apb = apbaccess.apbwriter(memfile,
                                  apb_base,
                                  block_mode,
                                  verify_writes,
                                  no_error_stop,
                                  weight_header=weight_header,
                                  sampledata_header=sampledata_header,
                                  embedded_code=embedded_code,
                                  compact_weights=compact_weights,
                                  compact_data=compact_data,
                                  write_zero_registers=write_zero_regs,
                                  weight_filename=weight_filename,
                                  sample_filename=sample_filename,
                                  device=device)

        apb.copyright_header()

        apb.output(f'// {test_name}\n')
        apb.output(f'// Created using {" ".join(str(x) for x in sys.argv)}\n')

        # Human readable description of test
        apb.output(f'\n// Configuring {layers} layer{"s" if layers > 1 else ""}:\n')

        for ll in range(layers):
            apb.output(f'// Layer {ll}: {input_chan[ll]}x{input_dim_str[ll]} ('
                       f'{"streaming " if streaming[ll] else ""}'
                       f'{"flattened " if flatten[ll] else ""}'
                       f'{"CHW/big data)" if big_data[ll] else "HWC/little data)"}, ')
            if pool[ll][0] > 1 or pool[ll][1] > 1:
                apb.output(f'{pool_str[ll]} {"avg" if pool_average[ll] else "max"} '
                           f'pool with stride {pool_stride_str[ll]}')
            else:
                apb.output(f'no pooling')
            if operator[ll] in [op.CONV1D, op.CONV2D]:
                conv_str = f', {op.string(operator[ll])} with kernel size ' \
                           f'{kernel_size_str[ll]}, '
            else:
                conv_str = ', no convolution, '
            apb.output(conv_str +
                       f'stride {stride_str[ll]}, '
                       f'pad {padding_str[ll]}, '
                       f'{output_chan[ll]}x{output_dim_str[ll]} output\n')

        apb.output('\n')
        apb.header()

        # Calculate the groups needed, and groups and processors used overall
        processors_used = 0
        group_map = []
        for ll in range(layers):
            bits = processor_map[ll]
            processors_used |= bits

            if input_chan[ll] > tc.dev.MAX_CHANNELS:
                print(f'Layer {ll} is configured for {input_chan[ll]} inputs, which exceeds '
                      f'the system maximum of {tc.dev.MAX_CHANNELS}.')
                sys.exit(1)
            if output_chan[ll] > tc.dev.MAX_CHANNELS:
                print(f'Layer {ll} is configured for {output_chan[ll]} outputs, which exceeds '
                      f'the system maximum of {tc.dev.MAX_CHANNELS}.')
                sys.exit(1)
            if popcount(processor_map[ll]) != in_expand_thresh[ll]:
                print(f'Layer {ll} has {input_chan[ll]} inputs with input expansion '
                      f'{in_expand[ll]}, threshold {in_expand_thresh[ll]}, but '
                      f'enabled processor map 0x{processor_map[ll]:016x} '
                      f'has {popcount(processor_map[ll])} bits instead of the '
                      f'expected number of {in_expand_thresh[ll]}.')
                sys.exit(1)
            if popcount(output_processor_map[ll]) != out_expand_thresh[ll]:
                print(f'Layer {ll} has {output_chan[ll]} outputs with output expansion '
                      f'{out_expand[ll]}, threshold {out_expand_thresh[ll]}, but '
                      f'processor output map 0x{output_processor_map[ll]:016x} '
                      f'has {popcount(output_processor_map[ll])} bits instead of the '
                      f'expected number of {out_expand_thresh[ll]}.')
                sys.exit(1)
            this_map = []
            for group in range(tc.dev.P_NUMGROUPS):
                if (processor_map[ll] >> group*tc.dev.P_NUMPRO) % 2**tc.dev.P_NUMPRO:
                    this_map.append(group)
            group_map.append(this_map)

        groups_used = []
        for group in range(tc.dev.P_NUMGROUPS):
            if ((processors_used |
                 output_processor_map[-1]) >> group*tc.dev.P_NUMPRO) % 2**tc.dev.P_NUMPRO:
                groups_used.append(group)

        if 0 not in groups_used:
            print('Group 0 is not used, this currently does not work.')
            sys.exit(1)

        if embedded_code or compact_data:
            # Pre-define data memory loader. Inline later when generating RTL sim.
            load.load(True, apb, big_data[0], processor_map[0], in_offset[0],
                      [input_chan[0], input_dim[0][0], input_dim[0][1]],
                      in_expand[0], in_expand_thresh[0],
                      data, padding[0], split=split, debug=debug)
        if embedded_code or compact_weights:
            # Pre-define the kernels and bias values
            kern_offs, kern_len = \
                kernels.load(verbose, True, device, apb, layers, operator,
                             kernel, kernel_size,
                             quantization, processor_map, output_processor_map,
                             input_chan, output_chan, out_expand, out_expand_thresh,
                             in_expand, in_expand_thresh, flatten, debug)
            bias_offs, bias_group, group_bias_max = \
                kbias.load(verbose, True, apb, layers, bias,
                           quantization, group_map, output_chan, debug)

        apb.load_header()

        # Initialize CNN registers

        if verbose:
            print('\nGlobal registers:')
            print('-----------------')

        # Disable completely unused groups
        for group in range(tc.dev.P_NUMGROUPS):
            if group not in groups_used:
                apb.write_ctl(group, tc.dev.REG_CTL, 0,
                              verbose, comment=f' // Disable group {group}')

        # Configure global control registers for used groups
        for _, group in enumerate(groups_used):
            if init_tram:
                # Zero out Tornado RAM
                if not embedded_code:
                    for p in range(tc.dev.P_NUMPRO):
                        for offs in range(tc.dev.TRAM_SIZE):
                            apb.write_tram(group, p, offs, 0, comment='Zero ')
                    apb.output('\n')
                else:
                    for p in range(tc.dev.P_NUMPRO):
                        addr = apb_base + tc.dev.C_GROUP_OFFS*group + tc.dev.C_TRAM_BASE \
                            + p * tc.dev.TRAM_OFFS * 4
                        apb.output(f'  memset((uint32_t *) 0x{addr:08x}, 0, '
                                   f'{tc.dev.TRAM_SIZE}); // Zero TRAM {group}\n')
                        apb.output('\n')

            # Stop state machine - will be overwritten later
            apb.write_ctl(group, tc.dev.REG_CTL, 0x06,
                          verbose, comment=' // Stop SM')
            # SRAM Control - does not need to be changed
            apb.write_ctl(group, tc.dev.REG_SRAM, 0x40e,
                          verbose, comment=' // SRAM control')
            # Number of layers
            apb.write_ctl(group, tc.dev.REG_LCNT_MAX, layers-1,
                          verbose, comment=' // Layer count')
            apb.output('\n')

        if not (embedded_code or compact_weights):
            kern_offs, kern_len = \
                kernels.load(verbose, embedded_code, device, apb, layers, operator,
                             kernel, kernel_size,
                             quantization, processor_map, output_processor_map,
                             input_chan, output_chan, out_expand, out_expand_thresh,
                             in_expand, in_expand_thresh, flatten, debug)
            bias_offs, bias_group, group_bias_max = \
                kbias.load(verbose, embedded_code, apb, layers, bias,
                           quantization, group_map, output_chan, debug)
        else:
            apb.output('  load_kernels();\n')
            if max(group_bias_max) > 0:
                apb.output('  load_bias();\n')

        if verbose:
            print('\nGlobal configuration:')
            print('---------------------')
            print(f'Used processors     = 0x{processors_used:016x}')
            print(f'Used groups         = {groups_used}')

            print('\nPer-group configuration:')
            print('-----------------------')
            print(f'Used bias memory    = {group_bias_max}')

            print('\nPer-layer configuration:')
            print('------------------------')
            print(f'Input dimensions    = {input_dim}')
            print(f'Input channels      = {input_chan}')
            print(f'Flatten             = {flatten}')
            print('Processor map       = [',
                  ', '.join('0x{:016x}'.format(k) for k in processor_map), ']', sep='',)
            if device != 84:
                print(f'Input expansion     = {in_expand}')
                print(f'Expansion threshold = {in_expand_thresh}')
            print('Input offsets       = [',
                  ', '.join('0x{:04x}'.format(k) for k in in_offset), ']', sep='',)

            print(f'Output dimensions   = {output_dim}')
            print(f'Output channels     = {output_chan}')
            print('Output processors   = [',
                  ', '.join('0x{:016x}'.format(k) for k in output_processor_map), ']', sep='',)
            if device != 84:
                print(f'Output expansion    = {out_expand}')
                print(f'Expansion threshold = {out_expand_thresh}')
                print(f'Output data bits    = {output_width}')
            print('Output offsets      = [',
                  ', '.join('0x{:04x}'.format(k) for k in out_offset), ']', sep='',)

            print(f'Group map           = {group_map}')

            print(f'Kernel offsets      = {kern_offs}')
            print(f'Kernel lengths      = {kern_len}')
            if device != 84:
                print(f'Kernel dimensions   = {kernel_size}')
                print(f'Kernel size         = {quantization}')
            print(f'Operator            = [',
                  ', '.join(op.string(k) for k in operator), ']', sep='',)
            print(f'Stride              = {stride}')

            print(f'Padding             = {padding}')
            print(f'Group with bias     = {bias_group}')
            print(f'Bias offsets        = {bias_offs}')
            print(f'Pooling             = {pool}')
            print(f'Pooling stride      = {pool_stride}')
            print(f'Pooled dimensions   = {pooled_dim}')
            print(f'Streaming           = {streaming}')
            print('')

        if verbose:
            print('Layer register configuration:')
            print('-----------------------------')

        # Configure per-layer control registers
        for ll in range(layers):

            local_source = operator[ll] == op.NONE
            for _, group in enumerate(groups_used):
                # Local output must be used:
                # - When parallel processing is enabled (not currently supported), or
                # - When there are gaps in the output, and
                #   - the gaps are non-uniform, or
                #   - the layer is in passthrough mode
                # Uniform gaps (when not in passthrough mode) can be achieved using the
                # time slot offset.

                if local_source:
                    break

                gap_max, gap_min = 0, tc.dev.MAX_PROC
                gmap = output_processor_map[ll] & 2**tc.dev.P_NUMPRO - 1 << group*tc.dev.P_NUMPRO
                if popcount(gmap) > 1:
                    p = ffs(gmap)
                    while p < fls(gmap):
                        gap = ffs(gmap & ~(2**(p+1) - 1)) - p - 1
                        gap_min, gap_max = min(gap, gap_min), max(gap, gap_max)
                        p += gap + 1
                    local_source = gap_min != gap_max or gap_max > 0 and operator[ll] == op.NONE

                # FIXME: Check that we don't overlap by-16 groups when in local_source mode
                # FIXME: Non-uniform gaps are not supported

            for _, group in enumerate(groups_used):
                apb.output(f'\n  // Layer {ll} group {group}\n')

                if device == 84 and operator[ll] == op.CONV1D:
                    # For 1D convolutions on AI84, the column count is always 3, and the
                    # row count is divided by 3. Padding is divided by 3.
                    val = (padding[ll][0] // 3 << 8) \
                           | (input_dim[ll][0] + 2*padding[ll][0]) // 3 - 1
                    apb.write_lreg(group, ll, tc.dev.LREG_RCNT, val,
                                   verbose, comment=' // Rows')
                    apb.write_lreg(group, ll, tc.dev.LREG_CCNT, 2,
                                   verbose, comment=' // Columns')
                else:
                    # Configure row count
                    # [7:0] maxcount: lower 8 bits = total of width + pad - 1
                    # [9:8] pad: 2 bits pad
                    apb.write_lreg(group, ll, tc.dev.LREG_RCNT,
                                   padding[ll][0] << 8 | input_dim[ll][0]-1 + 2*padding[ll][0],
                                   verbose, comment=' // Rows')

                    # Configure column count (evaluates to 0 for 1D convolutions)
                    # [7:0] width including padding - 1
                    # [9:8] pad count (0 = no pad, 1 = half pad, 2 = full pad)
                    apb.write_lreg(group, ll, tc.dev.LREG_CCNT,
                                   padding[ll][1] << 8 | input_dim[ll][1]-1 + 2 * padding[ll][1],
                                   verbose, comment=' // Columns')

                if operator[ll] != op.CONV2D and device != 84 or \
                   operator[ll] == op.CONV2D and kernel_size[ll] == [1, 1]:
                    #  [3:0] tscnt_max[3:0]      Maximum timeslot count register
                    #  [7:4] oned_sad[3:0]       Start mask address (offset within 9 byte mask)
                    # [11:8] oned_width[3:0]     1D mask width (0-9). Width > 0 enables 1D.
                    #   [12] oned_iena           Input data is 1-dimensional
                    if operator[ll] == op.CONV1D:
                        val = kernel_size[ll][0] << 8 | 1 << 12
                    else:
                        val = 1 << 8
                    apb.write_lreg(group, ll, tc.dev.LREG_ONED, val,
                                   verbose, comment=' // 1D')

                # Configure pooling row count
                val = pool[ll][0]-1
                if device == 84 and pool[ll][0] == 1:
                    val = 1
                else:
                    val = pool[ll][0]-1
                apb.write_lreg(group, ll, tc.dev.LREG_PRCNT, val,
                               verbose, comment=' // Pooling rows')

                # Configure pooling column count
                if device == 84 and pool[ll][1] == 1:
                    val = 1
                else:
                    val = pool[ll][1]-1
                apb.write_lreg(group, ll, tc.dev.LREG_PCCNT, val,
                               verbose, comment=' // Pooling columns')

                # Configure pooling stride count
                if pool[ll][0] > 1 or pool[ll][1] > 1:
                    val = pool_stride[ll][0]-1
                else:
                    val = stride[ll][0]-1
                if device == 84 and operator[ll] == op.CONV1D:
                    val //= 3
                apb.write_lreg(group, ll, tc.dev.LREG_STRIDE, val,
                               verbose, comment=' // Stride')

                val = out_offset[ll] // 4
                if not local_source:
                    # Configure SRAM write pointer -- write ptr is global
                    # Get offset to first available instance of the first used processor of the
                    # next layer.
                    instance = ffs(output_processor_map[ll]) & ~(tc.dev.P_SHARED-1)
                    val |= (instance % tc.dev.P_SHARED) * tc.dev.INSTANCE_SIZE \
                        | (instance // tc.dev.P_SHARED) << tc.dev.INSTANCE_SHIFT
                else:
                    instance = ffs(output_processor_map[ll] >> group * tc.dev.P_SHARED) \
                           & ~(tc.dev.P_SHARED-1)
                    val |= (instance + group * tc.dev.P_SHARED) * tc.dev.INSTANCE_SIZE
                apb.write_lreg(group, ll, tc.dev.LREG_WPTR_BASE, val,
                               verbose, comment=' // SRAM write ptr')

                if device == 84:
                    # Configure write pointer mask offset count
                    # [15:0]  Timeslot offset
                    #         [11:0]  12 bits for memory - word address every time we reach limit
                    #         [13:12] instance in group
                    #         [15:14] by-16 group
                    # [31:16] Mask offset (0x10000000, required when writing more than 4 masks)
                    if input_chan[ll] * kern_len[ll] > 4:
                        val = 1 << tc.dev.INSTANCE_SHIFT + 16
                    else:
                        val = 0
                    apb.write_lreg(group, ll, tc.dev.LREG_WPTR_OFFS, val,
                                   verbose, comment=' // Mask offset count')
                else:
                    # [15:0] Write Pointer Timeslot Offset Register
                    # Used for 1x1 convolution, and pooling without convolution
                    if operator[ll] == op.CONV2D and kernel_size[ll] == [1, 1]:
                        val = 1
                    elif operator[ll] not in [op.CONV1D, op.CONV2D]:
                        val = tc.dev.INSTANCE_SIZE
                    else:
                        val = 0
                    apb.write_lreg(group, ll, tc.dev.LREG_WPTR_TOFFS, val,
                                   verbose, comment=' // Write ptr time slot offs')

                    # [15:0] Write Pointer Mask Offset Register
                    val = 1 << tc.dev.INSTANCE_SHIFT
                    apb.write_lreg(group, ll, tc.dev.LREG_WPTR_MOFFS, val,
                                   verbose, comment=' // Write ptr mask offs')

                    # [15:0] Write Pointer Multi-Pass Channel Offset Register
                    val = output_width[ll] // 8
                    apb.write_lreg(group, ll, tc.dev.LREG_WPTR_CHOFFS, val,
                                   verbose, comment=' // Write ptr multi-pass channel offs')

                # Configure sram read ptr count -- read ptr is local
                # Source address must match write pointer of previous layer (minus global offset)
                apb.write_lreg(group, ll, tc.dev.LREG_RPTR_BASE,
                               in_offset[ll] // 4,
                               verbose, comment=' // SRAM read ptr')

                # Configure per-layer control
                # [3:0] s_slave: enable the by-4 group within the by-16 mask RAM to slave
                #                to first input volume; also enable timeslot
                # [4]   m_slave: slaves to 16x masters
                # [5]   master: sums all 16 processor outputs (vs 4 sums)
                # [6]   parallel: equals CHW/big data (per layer control)
                # [7]   pool_enable
                # [8]   maxpool_enable
                # [9]   activation_enable
                # [10]  cpad_only (column pad only, no row pad) for parallel processing
                # [11]  sramlsrc: global/local output SRAM data memory input select
                # [15:12] cnnsiena: enable externally sourced summed values from other processors
                # [21:19] wptr_inc (AI85 only)
                # [30:22] xpch_max (AI85 only) Selects the maximum channel processor number used
                #                  in channel expansion mode (bottom 3 are for bits)
                # [31]  bigdwrt (AI85 only) Enables 32-bit output
                val = (0x200 if activate[ll] else 0) | \
                      (0x100 if not pool_average[ll] else 0) | \
                      (0x80 if pool[ll][0] > 1 or pool[ll][1] > 1 else 0) | \
                      (0x40 if big_data[ll] else 0) | \
                      (0x20)
                if not local_source:
                    val |= 0x800
                if device != 84:
                    val |= ((out_expand[ll] - 1) << 19) \
                           | (fls(output_processor_map[ll])
                              - (ffs(output_processor_map[ll]) & ~(tc.dev.P_SHARED-1))) \
                           * quantization[ll] << 22 \
                           | (in_expand[ll] - 1) << 16
                    if output_width[ll] != 8:
                        val |= 1 << 31
                if group == groups_used[0]:
                    # Set external source for other active processing groups (can be zero if no
                    # other groups are processing). Do not set the bit corresponding to this group
                    # (e.g., if group == 0, do not set bit 12)
                    sources = 0
                    for t in range(groups_used[0]+1, tc.dev.P_NUMGROUPS):
                        # See if any processors other than this one are operating
                        # and set the cnnsiena bit if true
                        if (processor_map[ll] >> (t * tc.dev.P_NUMPRO)) % 2**tc.dev.P_NUMPRO:
                            sources |= 1 << t
                    val |= sources << 12
                apb.write_lreg(group, ll, tc.dev.LREG_LCTL, val,
                               verbose, comment=' // Layer control')

                # Configure mask count
                # Restriction: Every one of the mask memories will have to start from same offset
                # AI84:
                # [6:0]   Max count (output channels)
                # [7]     RFU
                # [14:8]  Starting address for group of 16
                # [15]    RFU
                # [23:16] Bias pointer starting address
                # [24]    Bias enable
                # [31:25] RFU
                # AI85:
                # [15:0]  Max count (output channels)
                # [31:16] Starting address for group of 16
                if device == 84:
                    val = kern_offs[ll] << tc.dev.MCNT_SAD_OFFS \
                        | (kern_len[ll] << tc.dev.MCNT_MAX_OFFS) - 1
                    if group == bias_group[ll]:
                        # Enable bias only for one group
                        val |= 0x1000000 | bias_offs[ll] << 16
                else:
                    if operator[ll] != op.NONE:
                        kl = (((fls(output_processor_map[ll])
                                - (ffs(output_processor_map[ll]) & ~(tc.dev.P_SHARED-1))) + 1)
                              * quantization[ll]) * out_expand[ll] * in_expand[ll] \
                              - quantization[ll] + kern_offs[ll] * 8  # kern_offs is always bytes
                        val = kern_offs[ll] * 8 << tc.dev.MCNT_SAD_OFFS \
                            | kl << tc.dev.MCNT_MAX_OFFS  # kern_offs is always bytes
                    else:
                        val = 0
                apb.write_lreg(group, ll, tc.dev.LREG_MCNT, val,
                               verbose, comment=' // Mask offset and count')

                # Configure tram pointer max
                if operator[ll] == op.CONV1D or \
                   operator[ll] == op.CONV2D and kernel_size[ll] == [1, 1]:
                    val = 0
                else:
                    val = tram_max[ll] - 1
                    if ll > 0 and streaming[ll]:
                        prev_max = sum(tram_max[:ll])
                        val += prev_max
                        val |= prev_max << 16
                apb.write_lreg(group, ll, tc.dev.LREG_TPTR, val,
                               verbose, comment=' // TRAM ptr max')

                if device != 84:
                    # Compensate for the smaller weights by adjusting the output shift
                    if quantization[ll] == 1:
                        val = 1 << 22 | 3 << 13  # Shift left 3
                    elif quantization[ll] == 2:
                        val = 2 << 22 | 2 << 13  # Shift left 2
                    elif quantization[ll] == 4:
                        val = 3 << 22 | 1 << 13  # Shift left 1
                    else:
                        assert quantization[ll] == 8
                        val = 0  # Do not shift

                    # [24] ts_ena
                    # [25] onexone_ena

                    if group == bias_group[ll]:
                        # Enable bias only for one group
                        val |= 1 << 12 | bias_offs[ll]

                    if operator[ll] == op.NONE:
                        val |= 3 << 24

                    apb.write_lreg(group, ll, tc.dev.LREG_POST, val,
                                   verbose, comment=' // AI85/86 post processing register')

                # Configure mask and processor enables
                # [15:0]  processor enable
                # [31:16] mask enable
                # When the input data is sourced from 16 independent byte streams, all 16
                # processors and compute elements need to be enabled.  If there were only 4 input
                # channels, 0x000f000f would be correct.
                #
                # Enable at most 16 processors and masks
                val = (processor_map[ll] >> group*tc.dev.P_NUMPRO) % 2**tc.dev.P_NUMPRO
                if operator[ll] in [op.CONV1D, op.CONV2D]:
                    val = val << 16 | val
                apb.write_lreg(group, ll, tc.dev.LREG_ENA, val,
                               verbose, comment=' // Mask and processor enables')

                if ll > 0 and streaming[ll]:
                    # [11:0]:  strm_isval[11:0]  Per stream start count - based on previous layer
                    #                            tptr_inc count
                    # [16:12]: strm_dsval1[4:0]  Per stream in-row delta count - based on previous
                    #                            layer tptr_inc count
                    # [31:20]: strm_dsval2[11:0] Per stream multi-row delta count - based on
                    #                            previous layer tptr_inc count
                    # Start count – defines the current layer rcnt (TRAM shift count) that
                    # triggers processing of the next layer
                    # Delta1 count – defines the current layer count once the start count is
                    # triggered that enables incremental layer processing.  This count is
                    # used when layer processing is contained within a single row.
                    # Delta2 count – defines the current layer count once the start count is
                    # triggered that enables incremental layer processing.  This count is
                    # used when layer processing spans multiple rows.

                    # Start: Prior layer's padded pooled row width * prior layer's kernel height
                    # + prior layer's kernel width + prior layer's pad
                    stream_start = (pooled_dim[ll-1][1] + 2 * padding[ll-1][1]) \
                        * kernel_size[ll-1][0] + padding[ll-1][1] + kernel_size[ll-1][1]
                    # Delta 1: This layer's pooling stride
                    delta1 = pool_stride[ll][1]
                    # Delta 2: (This layer's pooling - 1) * full prior layer's padded rows + prior
                    # layer's pad
                    delta2 = (pool[ll][0] - 1) * (pooled_dim[ll-1][1] + 2 * padding[ll-1][1]) \
                        + 2 * padding[ll-1][1]
                    val = delta2 << 20 | delta1 << 12 | stream_start
                    apb.write_lreg(group, ll, tc.dev.LREG_STREAM1, val,
                                   verbose, comment=' // Stream processing 1')
                    # [3:0]:   strm_invol[3:0]   Per stream invol offset - based on stream count
                    val = sum(in_expand[:ll])
                    apb.write_lreg(group, ll, tc.dev.LREG_STREAM2, val,
                                   verbose, comment=' // Stream processing 2')

        if zero_unused:
            for ll in range(layers, tc.dev.MAX_LAYERS):
                for _, group in enumerate(groups_used):
                    for reg in range(tc.dev.MAX_LREG+1):
                        if reg == tc.dev.LREG_RFU:  # Register 2 not implemented
                            continue
                        apb.write_lreg(group, ll, reg, 0,
                                       verbose, force_write=True,
                                       comment=f' // Zero unused layer {ll} registers')

        # Load data memory
        if embedded_code or compact_data:
            # Do the actual code generation later
            apb.output('\n  load_input(); // Load data input\n\n')
        else:
            load.load(embedded_code, apb, big_data[0], processor_map[0], in_offset[0],
                      [input_chan[0], input_dim[0][0], input_dim[0][1]],
                      in_expand[0], in_expand_thresh[0],
                      data, padding[0], split=split, debug=debug)

        if verbose:
            print('\nGlobal registers:')
            print('-----------------')

        # [0]    enable
        # [2:1]  rdy_sel  (wait states - set to max)
        # [3]    RFU
        # [4]    calcmax
        # [5]    poolena
        # [6]    bigdata
        # [7]    actena
        # [8]    one-shot (stop after single layer)
        # [11:9] ext_sync (slave to other group)
        # [12]   irq
        # [14]   strm_ena -  cnn_ctl register bit 14. Master stream processor enable. Layers are
        #        processed up to the first layer with a zero start count value. After the last
        #        stream layer (non-zero start and delta >> values) processing is complete,
        #        standard processing follows for the remaining layers.
        val = 1 << 14 if any(streaming) else 0

        # Enable all needed groups except the first one
        for _, group in enumerate(groups_used[1:]):
            apb.write_ctl(group, tc.dev.REG_CTL, val | 0x807 | groups_used[0] << 9,
                          verbose, comment=f' // Enable group {group}')

        # Master control - go
        apb.write_ctl(groups_used[0], tc.dev.REG_CTL, val | 0x07,
                      verbose, comment=f' // Master enable group {groups_used[0]}')

        apb.load_footer()
        # End of input

    in_map = apb.get_mem()

    if verbose:
        print('')

    # Compute layer-by-layer output and chain results into input
    for ll in range(layers):
        if operator[ll] == op.CONV2D:
            if flatten[ll]:
                in_chan = input_chan[ll] * input_dim[ll][0] * input_dim[ll][1]
                data = data.reshape(in_chan, 1, 1)
            else:
                in_chan = input_chan[ll]
                data = data.reshape(in_chan, input_dim[ll][0], input_dim[ll][1])
            out_buf, out_size = cnn2d_layer(ll,
                                            verbose,
                                            data.shape,
                                            kernel_size[ll], quantization[ll],
                                            output_chan[ll],
                                            padding[ll], dilation[ll],
                                            stride[ll],
                                            pool[ll],
                                            pool_stride[ll],
                                            pool_average[ll],
                                            activate[ll],
                                            kernel[ll].reshape(output_chan[ll], in_chan,
                                                               kernel_size[ll][0],
                                                               kernel_size[ll][1]),
                                            bias[ll],
                                            data,
                                            output_width=output_width[ll],
                                            device=device,
                                            debug=debug_computation,
                                            expand=in_expand[ll],
                                            expand_thresh=in_expand_thresh[ll])
        elif operator[ll] == op.CONV1D:
            data = data.reshape(input_chan[ll], input_dim[ll][0])
            out_buf, out_size = cnn1d_layer(ll,
                                            verbose,
                                            data.shape,
                                            kernel_size[ll][0], quantization[ll],
                                            output_chan[ll],
                                            padding[ll][0], dilation[ll][0],
                                            stride[ll][0],
                                            pool[ll][0],
                                            pool_stride[ll][0],
                                            pool_average[ll],
                                            activate[ll],
                                            kernel[ll].reshape(output_chan[ll], input_chan[ll],
                                                               kernel_size[ll][0]),
                                            bias[ll],
                                            data,
                                            output_width=output_width[ll],
                                            device=device,
                                            debug=debug_computation)
        elif operator[ll] == op.NONE:  # '0'D (pooling only or passthrough)
            data = data.reshape(input_chan[ll], input_dim[ll][0], input_dim[ll][1])
            out_buf, out_size = passthrough_layer(ll,
                                                  verbose,
                                                  data.shape,
                                                  pool[ll],
                                                  pool_stride[ll],
                                                  pool_average[ll],
                                                  data,
                                                  device=device,
                                                  debug=debug_computation,
                                                  expand=in_expand[ll],
                                                  expand_thresh=in_expand_thresh[ll])
        elif operator[ll] in [op.ELTWISE_ADD, op.ELTWISE_SUB, op.ELTWISE_MUL, op.ELTWISE_XOR]:
            data = data.reshape(input_chan[ll], input_dim[ll][0], input_dim[ll][1])
            out_buf, out_size = eltwise_layer(operator[ll],
                                              ll,
                                              verbose,
                                              data.shape,
                                              activate[ll],
                                              data,
                                              data,
                                              output_width=output_width[ll],
                                              device=device,
                                              debug=debug_computation,
                                              expand=in_expand[ll],
                                              expand_thresh=in_expand_thresh[ll])
        else:
            print(f'Unknown operator `{op.string(operator[ll])}`.')
            sys.exit(1)

        assert out_size[0] == output_chan[ll] \
            and out_size[1] == output_dim[ll][0] and out_size[2] == output_dim[ll][1]

        # Write .mem file for output or create the C cnn_check() function to verify the output
        out_map = [None] * tc.dev.C_GROUP_OFFS * tc.dev.P_NUMGROUPS
        if block_mode:
            if ll == layers-1:
                filename = output_filename + '.mem'  # Final output
            else:
                filename = f'{output_filename}-{ll+1}.mem'  # Intermediate output
            filemode = 'w'
        else:
            if ll == layers-1:
                filename = c_filename + '.c'  # Final output
            else:
                filename = None  # Intermediate output - used for layer overwrite check
            filemode = 'a'

        try:
            if filename:
                memfile = open(os.path.join(base_directory, test_name, filename), mode=filemode)
            else:
                memfile = None
            apb.set_memfile(memfile)

            apb.output(f'// {test_name}\n// Expected output of layer {ll}\n')
            apb.verify_header()
            apb.verify_unload(ll, in_map, out_map,
                              out_buf, output_processor_map[ll], out_size,
                              out_offset[ll], out_expand[ll],
                              out_expand_thresh[ll], output_width[ll],
                              pool[ll], pool_stride[ll], overwrite_ok, no_error_stop)
            apb.verify_footer()
        finally:
            if memfile:
                memfile.close()

        data = out_buf.reshape(out_size)
        in_map = out_map

    with open(os.path.join(base_directory, test_name, filename), mode=filemode) as memfile:
        apb.set_memfile(memfile)

        if fc_weights:
            data = data.flatten()

            apb.unload(output_processor_map[-1], out_size, out_offset[layers-1],
                       out_expand[-1], out_expand_thresh[-1], output_width[-1],
                       pool[-1], pool_stride[-1])

            out_buf, out_size = linear_layer(verbose=verbose,
                                             do_activation=False,
                                             data=data, weight=fc_weights[0], bias=fc_bias[0],
                                             debug=debug)

            apb.fc_layer(fc_weights[0], fc_bias[0])
            apb.fc_verify(out_buf)

        apb.main(fc_weights)

    # Close header files
    if embedded_code or compact_data:
        sampledata_header.close()
    if embedded_code or compact_weights:
        weight_header.close()

    # Create run_test.sv
    if not embedded_code:
        if not timeout:
            # If no timeout specified, calculate one based on reads/writes
            timeout = apb.get_time() + rtlsim.GLOBAL_TIME_OFFSET
        rtlsim.create_runtest_sv(block_mode, base_directory, test_name, runtest_filename,
                                 input_filename, timeout)

    return test_name


def main():
    """
    Command line wrapper
    """
    np.set_printoptions(threshold=np.inf, linewidth=190)

    args = commandline.get_parser()

    # Configure device
    tc.dev = tc.get_device(args.device)
    if args.apb_base:
        apb_base = args.apb_base
    else:
        apb_base = tc.dev.APB_BASE
    if args.max_proc:
        tc.dev.MAX_PROC = args.max_proc
        tc.dev.P_NUMPRO = args.max_proc
        tc.dev.P_NUMGROUPS = 1

    # Load configuration file
    cfg, params = yamlcfg.parse(args.config_file, args.device)

    # If not using test data, load weights and biases
    # This also configures the network's output channels
    if cfg['arch'] != 'test':
        if not args.checkpoint_file:
            print("--checkpoint-file is a required argument.")
            sys.exit(1)
        layers, weights, bias, fc_weights, fc_bias, input_channels, output_channels = \
            checkpoint.load(args.checkpoint_file, cfg['arch'],
                            args.fc_layer, params['quantization'])
    else:  # Get some hard-coded sample weights
        layers, weights, bias, fc_weights, fc_bias, input_channels, output_channels = \
            sampleweight.load(cfg['dataset'], params['quantization'], len(cfg['layers']),
                              cfg['bias'] if 'bias' in cfg else None)

    if layers != len(cfg['layers']):
        print(f"Number of layers in the YAML configuration file ({len(cfg['layers'])}) "
              f"does not match the checkpoint file ({layers}).")
        sys.exit(1)

    if any(p < 0 or p > 4*tc.dev.MEM_SIZE for p in params['output_offset']):
        print('Unsupported value for `out_offset` in YAML configuration.')
        sys.exit(1)

    if args.device == 84:
        if any(q != 8 for q in params['quantization']):
            print('All quantization configuration values must be 8 for AI84.')
            sys.exit(1)

        if any(p0 & 1 != 0 or p0 < 0 or p0 > 4 or p1 & 1 != 0
               or p1 < 0 or p1 > 4 for [p0, p1] in params['pool']):
            print('Unsupported value for `max_pool`/`avg_pool` for AI84 in YAML configuration.')
            sys.exit(1)

        if any(p0 == 3 or p0 < 0 or p0 > 4
               or p1 == 3 or p1 < 0 or p1 > 4 for [p0, p1] in params['pool_stride']):
            print('Unsupported value for `pool_stride` in YAML configuration.')
            sys.exit(1)

    if args.stop_after is not None:
        layers = args.stop_after + 1

    processor_map = params['processor_map']
    output_processor_map = params['output_processor_map'][:layers]

    if 'output_map' in cfg:
        # Use optional configuration value if it's specified
        output_processor_map[-1] = cfg['output_map']
    elif len(processor_map) == layers and output_processor_map[-1] is None:
        # Default to packed, 0-aligned output map
        expand = (output_channels[layers-1] + tc.dev.MAX_PROC-1) // tc.dev.MAX_PROC
        expand_chunk = (output_channels[layers-1] + expand-1) // expand
        if output_channels[layers-1] > tc.dev.MAX_PROC:
            expand_chunk = (expand_chunk + tc.dev.P_SHARED-1) & ~(tc.dev.P_SHARED-1)
        output_processor_map[-1] = 2**expand_chunk-1

    # Remove extraneous layer configuration values (when --stop-after is used)
    processor_map = processor_map[:layers]

    input_channels = input_channels[:layers]
    output_channels = output_channels[:layers]
    output_offset = params['output_offset'][:layers]
    conf_input_dim = params['input_dim'][:layers]
    input_offset = params['input_offset'][:layers]
    kernel_size = params['kernel_size'][:layers]
    quantization = params['quantization'][:layers]
    pool = params['pool'][:layers]
    pool_stride = params['pool_stride'][:layers]
    padding = params['padding'][:layers]
    stride = params['stride'][:layers]
    dilation = params['dilation'][:layers]
    big_data = params['big_data'][:layers]
    output_width = params['output_width'][:layers]
    operator = params['operator'][:layers]
    streaming = params['streaming'][:layers]
    flatten = params['flatten'][:layers]

    # Command line override
    if args.input_offset is not None:
        input_offset[0] = args.input_offset

    # Derived configuration options
    activate = [bool(x) for x in params['relu']]
    pool_average = [bool(x) for x in params['average']]

    print(f"Configuring data set: {cfg['dataset']}.")
    data = sampledata.get(cfg['dataset'])
    input_size = list(data.shape)

    # Trace output sizes of the network
    auto_input_dim = [None] * layers
    input_dim = [None] * layers
    pooled_dim = [None] * layers
    output_dim = [None] * layers

    if operator[0] != op.CONV1D:
        auto_input_dim[0] = [input_size[1], input_size[2]]
    else:
        auto_input_dim[0] = [input_size[1], 1]
    if conf_input_dim[0] is None:
        input_dim[0] = auto_input_dim[0]
    else:
        input_dim[0] = conf_input_dim[0]
    for ll in range(layers):
        assert weights[ll].min() >= -1 << quantization[ll] - 1
        assert weights[ll].max() <= (1 << quantization[ll] - 1) - 1

        if input_dim[ll] is None:
            auto_input_dim[ll] = output_dim[ll-1]
            if conf_input_dim[ll] is None:
                input_dim[ll] = auto_input_dim[ll]
            else:
                input_dim[ll] = conf_input_dim[ll]
        if pool[ll][0] > 1 or pool[ll][1] > 1:
            if operator[ll] != op.CONV1D:
                if pool_stride[ll][0] != pool_stride[ll][1]:
                    print(f'{op.string(operator[ll])} in layer {ll} does not support non-square'
                          f'pooling stride (currently set to '
                          f'{pool_stride[ll][0]}x{pool_stride[ll][1]}).')
                    sys.exit(1)
                pooled_size = [(input_dim[ll][0] + pool_stride[ll][0] - pool[ll][0])
                               // pool_stride[ll][0],
                               (input_dim[ll][1] + pool_stride[ll][1] - pool[ll][1])
                               // pool_stride[ll][1]]
            else:
                pooled_size = [(input_dim[ll][0] + pool_stride[ll][0] - pool[ll][0])
                               // pool_stride[ll][0],
                               1]
        else:
            pooled_size = input_dim[ll]
        if operator[ll] != op.CONV1D:
            if stride[ll][0] != stride[ll][1]:
                print(f'{op.string(operator[ll])} in layer {ll} does not support non-square '
                      f'stride (currently set to {stride[ll][0]}x{stride[ll][1]}).')
                sys.exit(1)
            if stride[ll][0] != 1:
                print(f'{op.string(operator[ll])} in layer {ll} does not support stride other '
                      f'than 1 (currently set to {stride[ll][0]}x{stride[ll][1]}).')
                sys.exit(1)
            output_dim[ll] = [(pooled_size[0] - dilation[ll][0] * (kernel_size[ll][0] - 1) - 1 +
                               2 * padding[ll][0]) // stride[ll][0] + 1,
                              (pooled_size[1] - dilation[ll][1] * (kernel_size[ll][1] - 1) - 1 +
                               2 * padding[ll][1]) // stride[ll][1] + 1]
            if flatten[ll]:
                output_dim[ll] = [1, 1]
                input_channels[ll] //= input_dim[ll][0] * input_dim[ll][1]
            if padding[ll][0] >= 3:
                print(f'{op.string(operator[ll])} in layer {ll} does not support `pad` >= 3 '
                      f'(currently set to {padding[ll][0]}).')
                sys.exit(1)
        else:
            # We don't have to consider padding for the width calculation,
            # since padding has to be a multiple of 3 and we check for that.
            if args.device == 84:
                if pooled_size[0] % 3 != 0:
                    print(f'{op.string(operator[ll])} in layer {ll} requires a multiple of 3 for'
                          f'the pooled input length (currently {pooled_size[0]}).')
                    sys.exit(1)
                if padding[ll][0] % 3 != 0:
                    print(f'{op.string(operator[ll])} in layer {ll} requires a multiple of 3 for '
                          f'`pad` (currently set to {padding[ll][0]}).')
                    sys.exit(1)
            else:
                if padding[ll][0] >= 3:
                    print(f'{op.string(operator[ll])} in layer {ll} does not support `pad` >= 3 '
                          f'(currently set to {padding[ll][0]}).')
                    sys.exit(1)
            output_dim[ll] = [(pooled_size[0] - dilation[ll][0] * (kernel_size[ll][0] - 1) - 1 +
                               2 * padding[ll][0]) // stride[ll][0] + 1,
                              1]
        pooled_dim[ll] = pooled_size

        # Prohibit pad greater than or equal to kernel size
        if padding[ll][0] >= kernel_size[ll][0] or padding[ll][1] >= kernel_size[ll][1]:
            print(f'Pad size for layer {ll} exceeds kernel size.')
            sys.exit(1)

        # Check for max dimensions
        if any(dim > tc.dev.MAX_ROW_COL for dim in input_dim[ll]):
            print(f'Input dimension {input_dim[ll]} exceeds system maximum of '
                  f'{tc.dev.MAX_ROW_COL} in layer {ll}.')
            sys.exit(1)
        if any(dim > tc.dev.MAX_ROW_COL for dim in output_dim[ll]):
            print(f'Output dimension {output_dim[ll]} exceeds system maximum of '
                  f'{tc.dev.MAX_ROW_COL} in layer {ll}.')
            sys.exit(1)

    if not args.cmsis_software_nn:
        tn = create_net(args.prefix,
                        args.verbose,
                        args.debug,
                        args.debug_computation,
                        args.no_error_stop,
                        args.overwrite_ok,
                        args.log,
                        apb_base,
                        layers,
                        operator,
                        input_dim,
                        pooled_dim,
                        output_dim,
                        processor_map,
                        output_processor_map,
                        kernel_size,
                        quantization,
                        input_channels,
                        output_channels,
                        output_width,
                        padding,
                        dilation,
                        stride,
                        pool,
                        pool_stride,
                        pool_average,
                        activate,
                        data,
                        weights,
                        bias,
                        big_data,
                        fc_weights,
                        fc_bias,
                        args.input_split,
                        input_offset,
                        output_offset,
                        streaming,
                        flatten,
                        args.input_filename,
                        args.output_filename,
                        args.c_filename,
                        args.test_dir,
                        args.runtest_filename,
                        args.log_filename,
                        args.zero_unused,
                        args.timeout,
                        not args.top_level,
                        args.verify_writes,
                        args.embedded_code,
                        args.compact_weights,
                        args.compact_data,
                        args.write_zero_registers,
                        args.weight_filename,
                        args.sample_filename,
                        args.device,
                        args.init_tram)
        if not args.embedded_code and args.autogen.lower() != 'none':
            rtlsim.append_regression(args.top_level,
                                     tn,
                                     args.queue_name,
                                     args.autogen)
    else:
        cmsisnn.create_net(args.prefix,
                           args.verbose,
                           args.debug,
                           args.log,
                           layers,
                           operator,
                           auto_input_dim,
                           input_dim,
                           pooled_dim,
                           output_dim,
                           kernel_size,
                           quantization,
                           input_channels,
                           output_channels,
                           output_width,
                           padding,
                           dilation,
                           stride,
                           pool,
                           pool_stride,
                           pool_average,
                           activate,
                           data,
                           weights,
                           bias,
                           fc_weights,
                           fc_bias,
                           args.c_filename,
                           args.test_dir,
                           args.log_filename,
                           args.weight_filename,
                           args.sample_filename,
                           args.device)

    print("SUMMARY OF OPS")
    stats.print_summary(args.debug)


def signal_handler(_signal, _frame):
    """
    Ctrl+C handler
    """
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    main()
