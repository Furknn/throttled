#!/usr/bin/env python3

import argparse
import configparser
import dbus
import os
import psutil
import struct
import subprocess
import sys

from collections import defaultdict
from dbus.mainloop.glib import DBusGMainLoop
from errno import EACCES, EPERM
from mmio import MMIO, MMIOError
from multiprocessing import cpu_count
from threading import Event, Thread

try:
    from gi.repository import GObject
except ImportError:
    import gobject as GObject

SYSFS_POWER_PATH = '/sys/class/power_supply/AC/online'

VOLTAGE_PLANES = {
    'CORE': 0,
    'GPU': 1,
    'CACHE': 2,
    'UNCORE': 3,
    'ANALOGIO': 4,
}

TRIP_TEMP_RANGE = [40, 97]
C_TDP_RANGE = (0, 2)

power = {'source': None, 'method': 'polling'}


def writemsr(msr, val):
    msr_list = ['/dev/cpu/{:d}/msr'.format(x) for x in range(cpu_count())]
    if not os.path.exists(msr_list[0]):
        try:
            subprocess.check_call(('modprobe', 'msr'))
        except subprocess.CalledProcessError:
            print('[E] Unable to load the msr module.')
            sys.exit(1)
    try:
        for addr in msr_list:
            f = os.open(addr, os.O_WRONLY)
            os.lseek(f, msr, os.SEEK_SET)
            os.write(f, struct.pack('Q', val))
            os.close(f)
    except (IOError, OSError) as e:
        if e.errno == EPERM or e.errno == EACCES:
            print('[E] Unable to write to MSR. Try to disable Secure Boot.')
            sys.exit(1)
        else:
            raise e


# returns the value between from_bit and to_bit as unsigned long
def readmsr(msr, from_bit=0, to_bit=63, cpu=None, flatten=False):
    assert cpu is None or cpu in range(cpu_count())
    if from_bit > to_bit:
        print('[E] Wrong readmsr bit params')
        sys.exit(1)
    msr_list = ['/dev/cpu/{:d}/msr'.format(x) for x in range(cpu_count())]
    if not os.path.exists(msr_list[0]):
        try:
            subprocess.check_call(('modprobe', 'msr'))
        except subprocess.CalledProcessError:
            print('[E] Unable to load the msr module.')
            sys.exit(1)
    try:
        output = []
        for addr in msr_list:
            f = os.open(addr, os.O_RDONLY)
            os.lseek(f, msr, os.SEEK_SET)
            val = struct.unpack('Q', os.read(f, 8))[0]
            os.close(f)
            mask = sum(2**x for x in range(from_bit, to_bit + 1))
            output.append((val & mask) >> from_bit)
        if flatten:
            return output[0] if len(set(output)) == 1 else output
        return output[cpu] if cpu is not None else output
    except (IOError, OSError) as e:
        if e.errno == EPERM or e.errno == EACCES:
            print('[E] Unable to read from MSR. Try to disable Secure Boot.')
            sys.exit(1)
        else:
            raise e


def is_on_battery():
    with open(SYSFS_POWER_PATH) as f:
        return not bool(int(f.read()))


def calc_time_window_vars(t):
    # 0.000977 is the time unit of my CPU
    time_unit = 1.0 / 2**readmsr(0x606, 16, 19, cpu=0)
    for Y in range(2**5):
        for Z in range(2**2):
            if t <= (2**Y) * (1. + Z / 4.) * time_unit:
                return (Y, Z)
    raise ValueError('Unable to find a good combination!')


def undervolt(config):
    for plane in VOLTAGE_PLANES:
        write_value = calc_undervolt_msr(plane, config.getfloat('UNDERVOLT', plane))
        writemsr(0x150, write_value)
        if args.debug:
            write_value &= 0xFFFFFFFF
            writemsr(0x150, 0x8000001000000000 | (VOLTAGE_PLANES[plane] << 40))
            read_value = readmsr(0x150, flatten=True)
            print('[D] Undervolt plane {:s} - write {:#x} - read {:#x}'.format(plane, write_value, read_value))


def calc_undervolt_msr(plane, offset):
    assert offset <= 0
    assert plane in VOLTAGE_PLANES
    offset = int(round(offset * 1.024))
    offset = 0xFFE00000 & ((offset & 0xFFF) << 21)
    return 0x8000001100000000 | (VOLTAGE_PLANES[plane] << 40) | offset


def load_config():
    config = configparser.ConfigParser()
    config.read(args.config)

    # config values sanity check
    for power_source in ('AC', 'BATTERY'):
        for option in (
                'Update_Rate_s',
                'PL1_Tdp_W',
                'PL1_Duration_s',
                'PL2_Tdp_W',
                'PL2_Duration_S',
        ):
            config.set(power_source, option, str(max(0.1, config.getfloat(power_source, option))))

        trip_temp = config.getfloat(power_source, 'Trip_Temp_C')
        valid_trip_temp = min(TRIP_TEMP_RANGE[1], max(TRIP_TEMP_RANGE[0], trip_temp))
        if trip_temp != valid_trip_temp:
            config.set(power_source, 'Trip_Temp_C', str(valid_trip_temp))
            print('[!] Overriding invalid "Trip_Temp_C" value in "{:s}": {:.1f} -> {:.1f}'.format(
                power_source, trip_temp, valid_trip_temp))

    for plane in VOLTAGE_PLANES:
        value = config.getfloat('UNDERVOLT', plane)
        valid_value = min(0, value)
        if value != valid_value:
            config.set('UNDERVOLT', plane, str(valid_value))
            print('[!] Overriding invalid "UNDERVOLT" value in "{:s}" voltage plane: {:.0f} -> {:.0f}'.format(
                plane, value, valid_value))

    return config


def calc_reg_values(config):
    regs = defaultdict(dict)
    for power_source in ('AC', 'BATTERY'):
        if readmsr(0xce, 30, 30, cpu=0) != 1:
            print("[W] Setting temperature target is not supported by this CPU")
        else:
            # the critical temperature for my CPU is 100 'C
            critical_temp = readmsr(0x1a2, 16, 23, cpu=0)
            # update the allowed temp range to keep at least 3 'C from the CPU critical temperature
            global TRIP_TEMP_RANGE
            TRIP_TEMP_RANGE[1] = min(TRIP_TEMP_RANGE[1], critical_temp - 3)

            trip_offset = int(round(critical_temp - config.getfloat(power_source, 'Trip_Temp_C')))
            regs[power_source]['MSR_TEMPERATURE_TARGET'] = trip_offset << 24

        # 0.125 is the power unit of my CPU
        power_unit = 1.0 / 2**readmsr(0x606, 0, 3, cpu=0)
        PL1 = int(round(config.getfloat(power_source, 'PL1_Tdp_W') / power_unit))
        Y, Z = calc_time_window_vars(config.getfloat(power_source, 'PL1_Duration_s'))
        TW1 = Y | (Z << 5)

        PL2 = int(round(config.getfloat(power_source, 'PL2_Tdp_W') / power_unit))
        Y, Z = calc_time_window_vars(config.getfloat(power_source, 'PL2_Duration_s'))
        TW2 = Y | (Z << 5)

        regs[power_source]['MSR_PKG_POWER_LIMIT'] = PL1 | (1 << 15) | (TW1 << 17) | (PL2 << 32) | (1 << 47) | (
            TW2 << 49)

        # cTDP
        c_tdp_target_value = config.getint(power_source, 'cTDP', fallback=None)
        if c_tdp_target_value is not None:
            if readmsr(0xce, 33, 34, cpu=0) < 2:
                print("[W] cTDP setting not supported by this CPU")
            else:
                valid_c_tdp_target_value = min(C_TDP_RANGE[1], max(C_TDP_RANGE[0], c_tdp_target_value))
                regs[power_source]['MSR_CONFIG_TDP_CONTROL'] = valid_c_tdp_target_value
    return regs


def set_hwp(pref):
    # set HWP energy performance hints
    assert pref in ('performance', 'balance_performance', 'default', 'balance_power', 'power')
    CPUs = ['/sys/devices/system/cpu/cpu{:d}/cpufreq/energy_performance_preference'.format(x) for x in range(cpu_count())]
    for i, c in enumerate(CPUs):
        with open(c, 'wb') as f:
            f.write(pref.encode())
        if args.debug:
            with open(c) as f:
                print('[D] HWP for cpu{:d} - write "{:s}" - read "{:s}"'.format(i, pref, f.read().strip()))


def power_thread(config, regs, exit_event):
    try:
        mchbar_mmio = MMIO(0xfed159a0, 8)
    except MMIOError:
        print('[E] Unable to open /dev/mem. Try to disable Secure Boot.')
        sys.exit(1)

    while not exit_event.is_set():
        # switch back to sysfs polling
        if power['method'] == 'polling':
            power['source'] = 'BATTERY' if is_on_battery() else 'AC'

        # set temperature trip point
        if 'MSR_TEMPERATURE_TARGET' in regs[power['source']]:
            write_value = regs[power['source']]['MSR_TEMPERATURE_TARGET']
            writemsr(0x1a2, write_value)
            if args.debug:
                read_value = readmsr(0x1a2, 24, 29, flatten=True)
                print('[D] TEMPERATURE_TARGET - write {:#x} - read {:#x}'.format(write_value >> 24, read_value))

        # set cTDP
        if 'MSR_CONFIG_TDP_CONTROL' in regs[power['source']]:
            write_value = regs[power['source']]['MSR_CONFIG_TDP_CONTROL']
            writemsr(0x64b, write_value)
            if args.debug:
                read_value = readmsr(0x64b, 0, 1, flatten=True)
                print('[D] CONFIG_TDP_CONTROL - write {:#x} - read {:#x}'.format(write_value, read_value))

        # set PL1/2 on MSR
        write_value = regs[power['source']]['MSR_PKG_POWER_LIMIT']
        writemsr(0x610, write_value)
        if args.debug:
            read_value = readmsr(0x610, 0, 55, flatten=True)
            print('[D] MSR PACKAGE_POWER_LIMIT - write {:#x} - read {:#x}'.format(write_value, read_value))
        # set MCHBAR register to the same PL1/2 values
        mchbar_mmio.write32(0, write_value & 0xffffffff)
        mchbar_mmio.write32(4, write_value >> 32)
        if args.debug:
            read_value = mchbar_mmio.read32(0) | (mchbar_mmio.read32(4) << 32)
            print('[D] MCHBAR PACKAGE_POWER_LIMIT - write {:#x} - read {:#x}'.format(write_value, read_value))

        wait_t = config.getfloat(power['source'], 'Update_Rate_s')
        enable_hwp_mode = config.getboolean('AC', 'HWP_Mode', fallback=False)
        if power['source'] == 'AC' and enable_hwp_mode:
            cpu_usage = float(psutil.cpu_percent(interval=wait_t))
            # set full performance mode only when load is greater than this threshold (~ at least 1 core full speed)
            performance_mode = cpu_usage > 100. / (cpu_count() * 1.25)
            # check again if we are on AC, since in the meantime we might have switched to BATTERY
            if not is_on_battery():
                set_hwp('performance' if performance_mode else 'balance_performance')
        else:
            exit_event.wait(wait_t)


def main():
    global args

    if os.geteuid() != 0:
        print('[E] No root no party. Try again with sudo.')
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='add some debug info and additional checks')
    parser.add_argument('--config', default='/etc/lenovo_fix.conf', help='override default config file path')
    args = parser.parse_args()

    power['source'] = 'BATTERY' if is_on_battery() else 'AC'

    config = load_config()
    regs = calc_reg_values(config)

    if not config.getboolean('GENERAL', 'Enabled'):
        return

    exit_event = Event()
    thread = Thread(target=power_thread, args=(config, regs, exit_event))
    thread.daemon = True
    thread.start()

    undervolt(config)

    # handle dbus events for applying undervolt on resume from sleep/hybernate
    def handle_sleep_callback(sleeping):
        if not sleeping:
            undervolt(config)

    def handle_ac_callback(*args):
        try:
            power['source'] = 'BATTERY' if args[1]['Online'] == 0 else 'AC'
            power['method'] = 'dbus'
        except:
            power['method'] = 'polling'

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    # add dbus receiver only if undervolt is enabled in config
    if any(config.getfloat('UNDERVOLT', plane) != 0 for plane in VOLTAGE_PLANES):
        bus.add_signal_receiver(handle_sleep_callback, 'PrepareForSleep', 'org.freedesktop.login1.Manager',
                                'org.freedesktop.login1')
    bus.add_signal_receiver(
        handle_ac_callback,
        signal_name="PropertiesChanged",
        dbus_interface="org.freedesktop.DBus.Properties",
        path="/org/freedesktop/UPower/devices/line_power_AC")

    try:
        GObject.threads_init()
        loop = GObject.MainLoop()
        loop.run()
    except (KeyboardInterrupt, SystemExit):
        pass

    exit_event.set()
    loop.quit()
    thread.join(timeout=1)


if __name__ == '__main__':
    main()
