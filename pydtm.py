#!/usr/bin/env python

# Python (Euro)DOCSIS Traffic Meter
# Copyright (C) 2018 the contributors

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import ctypes
import fcntl
import logging
import os
import select
import time
import timeit
import socket

# DVB constants from Linux kernel files
DMX_IMMEDIATE_START = 0x4
DMX_IN_FRONTEND = 0x0
DMX_OUT_TS_TAP = 0x2
DMX_PES_OTHER = 0x14
DMX_SET_BUFFER_SIZE = 0x6f2d # ioctl
DMX_SET_PES_FILTER = 0x40146f2c # ioctl
DMX_STOP = 0x6f2a
DTV_DELIVERY_SYSTEM = 0x11
DTV_FREQUENCY = 0x3
DTV_INNER_FEC = 0x9
DTV_INVERSION = 0x6
DTV_MODULATION = 0x4
DTV_SYMBOL_RATE = 0x8
DTV_TUNE = 0x1
FEC_AUTO = 0x9
FE_READ_STATUS = -0x7ffb90bb # ioctl
FE_SET_PROPERTY = 0x40086f52 # ioctl
INVERSION_OFF = 0x0
QAM_256 = 0x5
QAM_64 = 0x3
SYS_DVBC_ANNEX_AC = 0x1

# mappings for DVB API data types - this code was copied
# more or less verbatim from: https://pypi.org/project/linuxdvb/
class dtv_property(ctypes.Structure):
    class _u(ctypes.Union):
        class _s(ctypes.Structure):
            _fields_ = [
                ("data", ctypes.c_uint8 * 32),
                ("len", ctypes.c_uint32),
                ("reserved1", ctypes.c_uint32 * 3),
                ("reserved2", ctypes.c_void_p)
            ]
        _fields_ = [
            ("data", ctypes.c_uint32),
            ("buffer", _s)
        ]
    _fields_ = [
        ("cmd", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
        ("u", _u),
        ("result", ctypes.c_int)
    ]
    _pack_ = True
class dtv_properties(ctypes.Structure):
    _fields_ = [
        ("num", ctypes.c_uint32),
        ("props", ctypes.POINTER(dtv_property))
    ]
class dvb_qam_parameters(ctypes.Structure):
    _fields_ = [
        ("symbol_rate", ctypes.c_uint32),
        ("fec_inner", ctypes.c_uint),
        ("modulation", ctypes.c_uint)
    ]
class dvb_frontend_parameters(ctypes.Structure):
    class _u(ctypes.Union):
        _fields_ = [
            ("qam", dvb_qam_parameters),
        ]
    _fields_ = [
        ("frequency", ctypes.c_uint32),
        ("inversion", ctypes.c_uint),
        ("u", _u)
    ]
class dvb_frontend_status(ctypes.Structure):
    _fields_ = [
        ("status", ctypes.c_uint),
    ]
class dmx_pes_filter_params(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint16),
        ("input", ctypes.c_uint),
        ("output", ctypes.c_uint),
        ("pes_type", ctypes.c_uint),
        ("flags", ctypes.c_uint32)
    ]
# end code copied from https://pypi.org/project/linuxdvb/


def init_logging():
    LOGGER.setLevel(logging.DEBUG)
    log_handler = logging.StreamHandler()
    log_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    log_handler.setFormatter(formatter)
    LOGGER.addHandler(log_handler)

def build_configuration():
    # parse command line arguments
    parser = argparse.ArgumentParser(
        description="pydtm - measure EuroDOCSIS 3.0 data rate",
        epilog="Note: By default, each frequency is scanned for step/num(frequencies) seconds. " \
               "All parameters can also be passed as environment variables, e.g. PYDTM_ADAPTER, " \
               "PYDTM_CARBON, PYDTM_DEBUG, PYDTM_FREQUENCIES, PYDTM_PREFIX, PYDTM_STEP and" \
               "PYTDM_TUNER."
    )
    parser.add_argument("-a", "--adapter", type=int, default=0,
                        help="use /dev/dvb/adapterN devices (default: 0)")
    parser.add_argument("-c", "--carbon", type=str, default="localhost:2003",
                        help="address:port of carbon sink (default: localhost:2003)")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="enable debug logging (default: not enabled)")
    parser.add_argument("-f", "--frequencies", type=str, default="546",
                        help=("a list of 'frequency' or 'frequency:modulation'"
                              "pairs (default: 546:256)"))
    parser.add_argument("-p", "--prefix", type=str, default="docsis",
                        help="carbon prefix/tree location (default: docsis)")
    parser.add_argument("-s", "--step", type=int, default="60",
                        help="metrics backend default resolution in seconds (default: 60)")
    parser.add_argument("-t", "--tuner", type=int, default=0,
                        help="use adapter's frontendN/dmxN/dvrN devices (default: 0)")
    args = parser.parse_args()

    # overwrite with environment values
    if "PYDTM_ADAPTER" in os.environ:
        LOGGER.debug("reading adapter from environment")
        try:
            args.adapter = int(os.environ["PYDTM_ADAPTER"])
        except ValueError:
            LOGGER.error("error parsing PYDTM_ADAPTER value %s as integer, using %d instead",
                         os.environ["PYDTM_ADAPTER"], args.adapter)
    if "PYDTM_CARBON" in os.environ:
        LOGGER.debug("reading carbon sink from environment")
        args.carbon = os.environ["PYDTM_CARBON"]
    if "PYDTM_DEBUG" in os.environ:
        LOGGER.debug("reading debug flag from environment")
        args.debug = True
    if "PYDTM_FREQUENCIES" in os.environ:
        LOGGER.debug("reading frequency list from environment")
        args.frequencies = os.environ["PYDTM_FREQUENCIES"]
    if "PYDTM_PREFIX" in os.environ:
        LOGGER.debug("reading carbon prefix/tree location from environment")
        args.frequencies = os.environ["PYDTM_PREFIX"]
    if "PYDTM_STEP" in os.environ:
        LOGGER.debug("reading metrics store resolution from environment")
        try:
            args.step = int(os.environ["PYDTM_STEP"])
        except ValueError:
            LOGGER.error("error parsing PYDTM_STEP value %s as integer, using %d instead",
                         os.environ["PYDTM_STEP"], args.step)
    if "PYDTM_TUNER" in os.environ:
        LOGGER.debug("reading tuner from environment")
        try:
            args.adapter = int(os.environ["PYDTM_TUNER"])
        except ValueError:
            LOGGER.error("error parsing PYDTM_TUNER value %s as integer, using %d instead",
                         os.environ["PYDTM_TUNER"], args.tuner)

    # generate a list of frequencies
    frequencies = []
    for freq in args.frequencies.split(","):
        if freq.find(":") < 0:
            try:
                frequencies.append((int(freq), QAM_256))
            except ValueError:
                LOGGER.critical("error parsing frequency %s as integer, aborting", freq)
                exit(1)
            LOGGER.debug("added frequency %sMHz", freq)
        else:
            freq, mod = freq.split(":")
            try:
                freq = int(freq)
            except ValueError:
                LOGGER.critical("error parsing frequency %s as string, aborting", freq)
                exit(1)
            if mod == "256":
                LOGGER.debug("adding frequency %sMHz with modulation QAM_%s", freq, mod)
                frequencies.append((freq, QAM_256))
            elif mod == "64":
                LOGGER.debug("adding frequency %sMHz with modulation QAM_%s", freq, mod)
                frequencies.append((freq, QAM_64))
            else:
                LOGGER.critical("invalid modulation QAM_%s detected, aborting", mod)
                exit(1)

    # generate carbon destination
    carbon_port = 2003
    carbon_host = "localhost"
    if args.carbon.find(":") > 0:
        carbon_host, carbon_port = args.carbon.split(":")
        try:
            carbon_port = int(carbon_port)
        except ValueError:
            LOGGER.critical("unable to parse port %s as an integer, aborting", carbon_port)
            exit(1)
    elif args.carbon.find(":") < 0:
        carbon_host = args.carbon
    else:
        LOGGER.error("invalid carbon sink, aborting")
        exit(1)


    # show all log settings
    LOGGER.debug("adapter=%d", args.adapter)
    LOGGER.debug("carbon=%s", args.carbon)
    LOGGER.debug("debug=%s", args.debug)
    LOGGER.debug("frequencies=%s", frequencies)
    LOGGER.debug("prefix=%s", args.prefix)
    LOGGER.debug("step=%d", args.step)
    LOGGER.debug("tuner=%d", args.tuner)

    # make sure we got at least one second per frequency
    if args.step / len(frequencies) < 1:
        LOGGER.error("A step of %d seconds with %d different frequencies will result in less " \
                     "than one second of scan time per frequency, which is not supported. " \
                     "Aborting", args.step, len(frequencies))
        exit(1)

    return args.adapter, (carbon_host, carbon_port), args.debug, frequencies, args.prefix, \
           args.step, args.tuner


def tune(fefd, frequency, modulation):
    LOGGER.debug("tuning to frequency %d with modulation %d", frequency, modulation)
    # we are about to issue 7 commands to the DVB frontend
    proptype = dtv_property * 7
    prop = proptype()
    # set delivery system to DVB-C
    prop[0].cmd = DTV_DELIVERY_SYSTEM
    prop[0].u.data = SYS_DVBC_ANNEX_AC
    # set modulation
    # TODO: support QAM_AUTO?
    prop[1].cmd = DTV_MODULATION
    prop[1].u.data = modulation
    # set EuroDOCSIS symbol rate
    prop[2].cmd = DTV_SYMBOL_RATE
    prop[2].u.data = 6952000
    # DOCSIS profiles always set frequency inversion to off
    prop[3].cmd = DTV_INVERSION
    prop[3].u.data = INVERSION_OFF
    # autodetect Forward Error Correction
    prop[4].cmd = DTV_INNER_FEC
    prop[4].u.data = FEC_AUTO
    # set frequency
    prop[5].cmd = DTV_FREQUENCY
    prop[5].u.data = frequency
    # tell the kernel to actually tune into the given frequency
    prop[6].cmd = DTV_TUNE
    dtv_props = dtv_properties()
    dtv_props.num = 7
    dtv_props.props = ctypes.pointer(prop[0])
    if fcntl.ioctl(fefd, FE_SET_PROPERTY, dtv_props) == 0:
        # determine wheter the frontend actually has a lock
        # FIXME: why do I need this?
        time.sleep(0.250)
        # make sure the FE has a lock
        festatus = dvb_frontend_status()
        if fcntl.ioctl(fefd, FE_READ_STATUS, festatus) == 0:
            if (festatus.status & 0x10) == 0:
                LOGGER.error("frontend has no lock")
                return -1
        else:
            LOGGER.error("FE_READ_STATUS failed, unable to verify signal lock")
            return -1
    else:
        LOGGER.error("FE_SET_PROPERTY failed, unable to tune")
        return -1
    LOGGER.debug("tuning successful")
    return 0

def start_demuxer(dmxfd):
    # DOCSIS uses the MPEG-TS Packet Identifier 8190
    # tell the demuxer to get us the transport stream
    LOGGER.debug("starting demuxer")
    pesfilter = dmx_pes_filter_params()
    pesfilter.pid = 8190
    pesfilter.input = DMX_IN_FRONTEND
    pesfilter.output = DMX_OUT_TS_TAP
    pesfilter.pes_type = DMX_PES_OTHER
    pesfilter.flags = DMX_IMMEDIATE_START
    if fcntl.ioctl(dmxfd, DMX_SET_PES_FILTER, pesfilter) != 0:
        LOGGER.error("unable to start demuxer")
        return -1
    LOGGER.debug("demuxer initialization successful")
    return 0

def stop_demuxer(dmxfd):
    LOGGER.debug("stopping demuxer")
    if fcntl.ioctl(dmxfd, DMX_STOP) != 0:
        LOGGER.error("DMX_STOP failed, unable to stop demuxer (erm, what?)")
        return -1
    return 0

def main():
    # initialize console LOGGER
    init_logging()

    # simulate frequency and modulation list
    adapter, carbon, debug, frequencies, prefix, step, tuner = build_configuration()

    # update log level
    if not debug:
        LOGGER.setLevel(logging.INFO)
    else:
        LOGGER.setLevel(logging.DEBUG)

    # open the frontend device, demuxer and DVR device
    LOGGER.debug("about to open adapter %s, tuner %d devices", adapter, tuner)
    adapter = "/dev/dvb/adapter" + str(adapter)
    try:
        fefd = open(adapter + "/frontend" + str(tuner), "r+")
        dmxfd = open(adapter +"/demux"     + str(tuner), "r+")
        dvrfd = open(adapter +"/dvr"       + str(tuner), "rb")
    except IOError:
        LOGGER.error("Unable to open devices, aborting.", exc_info=True)
        exit(1)

    # the demux device needs to be opened non blocking
    flag = fcntl.fcntl(dvrfd, fcntl.F_GETFL)
    fcntl.fcntl(dvrfd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

    # we will need to poll the DVR
    dvr_poller = select.poll()
    dvr_poller.register(dvrfd, select.POLLIN | select.POLLPRI)

    # create sending socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # set appropriate buffer size
    # MPEG-TS are chopped into (at most) 188 sections
    ts_length = 188
    ts_buffer = ts_length * 2048
    LOGGER.debug("setting demuxer buffer size to %d", ts_buffer)
    if fcntl.ioctl(dmxfd, DMX_SET_BUFFER_SIZE, ts_buffer) != 0:
        LOGGER.error("DMX_SET_BUFFER_SIZE failed, aborting")
        fefd.close()
        dmxfd.close()
        dvrfd.close()
        exit(1)

    # begin main loop
    LOGGER.debug("starting main event loop")
    while True:
        # prepare message array for sending to carbon
        carbon_messages = []
        # iterate over all given frequency and modulation paris
        for freq, mod in frequencies:
            # try tuning
            if tune(fefd, (freq * 1000000), mod) != 0:
                break

            # at this point, we can poll data from /dev/dvb/adapter0/dvr0,
            # which we will promptly do, defining a 2s timeout
            timeout = 2
            count = 0

            if start_demuxer(dmxfd) != 0:
                break

            start_time = timeit.default_timer()
            end_time = start_time
            # make sure we spend at most (step / number of frequencies) second per frequency
            LOGGER.debug("spending about %ds with data retrieval", (step / len(frequencies)))
            while (end_time - start_time) < (step / len(frequencies)):
                # interrupting a poll() system call will cause a traceback
                # using try/except will suppress that for SIGTERM, but not for SIGINT
                # (Python got it"s own SIGINT handler)
                try:
                    events = dvr_poller.poll(timeout * 1000)
                except IOError:
                    LOGGER.warn("event polling was interrupted", exc_info=True)
                    # try to stop the demuxer
                    stop_demuxer(dmxfd)
                    break

                for _, flag in events:
                    if flag & (select.POLLIN | select.POLLPRI):
                        data = dvrfd.read(ts_buffer)
                        count += len(data)
                        end_time = timeit.default_timer()
                        elapsed = (end_time - start_time)
            # record final end time
            end_time = timeit.default_timer()
            elapsed = (end_time - start_time)

            # stop filtering
            if stop_demuxer(dmxfd) != 0:
                break

            # append data to carbon message
            if mod == QAM_256:
                m_type = "qam256"
            else:
                m_type = "qam64"
            carbon_messages.append("{}.{}.{} {} {}".format(prefix, m_type, freq, \
                                   (count/elapsed), int(time.time())))
            # for debugging purposes, output data
            LOGGER.debug("frequency %d: spent %fs, got %d packets (%d bytes) equaling a rate of" \
                         "%fkBit/s", freq, elapsed, len(data)/ts_length, len(data), \
                         ((count*8)/elapsed)/1024)
        # send data
        for msg in carbon_messages:
            LOGGER.debug("sending to carbon: %s", msg)
            sock.sendto((msg + "\n").encode(), carbon)

    # close devices - will never be called :-)
    dvrfd.close()
    dmxfd.close()
    fefd.close()
    sock.close()

if __name__ == "__main__":
    LOGGER = logging.getLogger()
    main()
