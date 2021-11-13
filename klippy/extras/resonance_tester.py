# A utility class to test resonances of the printer
#
# Copyright (C) 2020  Dmitry Butyugin <dmbutyugin@google.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math, os, time
from . import adxl345, shaper_calibrate

class TestAxis:
    def __init__(self, axis=None, vib_dir=None):
        if axis is None:
            self._name = "axis=%.3f,%.3f" % (vib_dir[0], vib_dir[1])
        else:
            self._name = axis
        if vib_dir is None:
            self._vib_dir = (1., 0.) if axis == 'x' else (0., 1.)
        else:
            s = math.sqrt(sum([d*d for d in vib_dir]))
            self._vib_dir = [d / s for d in vib_dir]
    def matches(self, chip_axis):
        if self._vib_dir[0] and 'x' in chip_axis:
            return True
        if self._vib_dir[1] and 'y' in chip_axis:
            return True
        return False
    def get_name(self):
        return self._name
    def get_point(self, l):
        return (self._vib_dir[0] * l, self._vib_dir[1] * l)

def _parse_axis(gcmd, raw_axis):
    if raw_axis is None:
        return None
    raw_axis = raw_axis.lower()
    if raw_axis in ['x', 'y']:
        return TestAxis(axis=raw_axis)
    dirs = raw_axis.split(',')
    if len(dirs) != 2:
        raise gcmd.error("Invalid format of axis '%s'" % (raw_axis,))
    try:
        dir_x = float(dirs[0].strip())
        dir_y = float(dirs[1].strip())
    except:
        raise gcmd.error(
                "Unable to parse axis direction '%s'" % (raw_axis,))
    return TestAxis(vib_dir=(dir_x, dir_y))

class VibrationPulseTest:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.min_freq = config.getfloat('min_freq', 5., minval=1.)
        self.max_freq = config.getfloat('max_freq', 205.,
                                        minval=self.min_freq, maxval=200.)
        self.accel = config.getfloat('accel', None, above=0.)
        self.min_v = config.getfloat('min_velocity', None, above=0.)
        self.hz_per_sec = config.getfloat('hz_per_sec', 1.,
                                          minval=0.1, maxval=2.)

        self.probe_points = config.getlists('probe_points', seps=(',', '\n'),
                                            parser=float, count=3)
    def get_start_test_points(self):
        return self.probe_points
    def prepare_test(self, gcmd):
        self.freq_start = gcmd.get_float("FREQ_START", self.min_freq, minval=1.)
        self.freq_end = gcmd.get_float("FREQ_END", self.max_freq,
                                       minval=self.freq_start, maxval=200.)
        self.test_accel = gcmd.get_float("ACCEL", self.accel, above=0.)
        self.min_test_v = gcmd.get_float("MIN_VELOCITY", self.min_v, above=0.)
        self.hz_per_sec = gcmd.get_float("HZ_PER_SEC", self.hz_per_sec,
                                         above=0., maxval=2.)
    def run_test(self, axis, gcmd):
        reactor = self.printer.get_reactor()
        toolhead = self.printer.lookup_object('toolhead')
        X, Y, Z, E = toolhead.get_position()
        # Override maximum acceleration and acceleration to
        # deceleration based on the maximum test frequency
        systime = self.printer.get_reactor().monotonic()
        toolhead_info = toolhead.get_status(systime)
        old_max_v = toolhead_info['max_velocity']
        old_max_accel = toolhead_info['max_accel']
        old_max_accel_to_decel = toolhead_info['max_accel_to_decel']
        old_scv = toolhead_info['square_corner_velocity']
        test_accel = self.test_accel or old_max_accel_to_decel
        min_v = self.min_test_v or old_scv
        max_v = max(.25 * test_accel / self.freq_start, min_v)
        max_accel = max(test_accel, 4. * min_v * self.freq_end)
        self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%.3f"
                " ACCEL=%.3f ACCEL_TO_DECEL=%.3f" % (
                    max_v, max_accel, max_accel))
        oldL = test_accel / self.freq_start**2 / 32.
        sign = 1.
        freq = self.freq_start
        gcmd.respond_info("Testing frequency %.0f Hz" % (freq,))
        while freq <= self.freq_end + 0.000001:
            t_seg = .5 / freq
            V = max(test_accel * t_seg * .5, min_v)
            accel = 2. * V / t_seg
            toolhead.cmd_M204(self.gcode.create_gcode_command(
                "M204", "M204", {"S": accel}))
            L = .25 * accel * t_seg**2 - oldL
            dX, dY = axis.get_point(L)
            nX = X + sign * dX
            nY = Y + sign * dY
            toolhead.move([nX, nY, Z, E], V)
            sign = -sign
            old_freq = freq
            freq += t_seg * self.hz_per_sec * (accel / test_accel)**2
            oldL = L
            if math.floor(freq) > math.floor(old_freq):
                gcmd.respond_info("Testing frequency %.0f Hz" % (freq,))
                reactor.pause(reactor.monotonic() + 0.01)
        # Restore the original acceleration values
        self.gcode.run_script_from_command(
                "SET_VELOCITY_LIMIT VELOCITY=%.3f"
                " ACCEL=%.3f ACCEL_TO_DECEL=%.3f" % (
                    old_max_v, old_max_accel, old_max_accel_to_decel))
        toolhead.move([X, Y, Z, E], max_v)

class ResonanceTester:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.move_speed = config.getfloat('move_speed', 50., above=0.)
        self.test = VibrationPulseTest(config)
        if not config.get('accel_chip_x', None):
            self.accel_chip_names = [('xy', config.get('accel_chip').strip())]
        else:
            self.accel_chip_names = [
                ('x', config.get('accel_chip_x').strip()),
                ('y', config.get('accel_chip_y').strip())]
            if self.accel_chip_names[0][1] == self.accel_chip_names[1][1]:
                self.accel_chip_names = [('xy', self.accel_chip_names[0][1])]
        self.autocalibrate = config.getboolean('autocalibrate', False)
        self.max_smoothing = config.getfloat('max_smoothing', None, minval=0.05)

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command("MEASURE_AXES_NOISE",
                                    self.cmd_MEASURE_AXES_NOISE,
                                    desc=self.cmd_MEASURE_AXES_NOISE_help)
        self.gcode.register_command("TEST_RESONANCES",
                                    self.cmd_TEST_RESONANCES,
                                    desc=self.cmd_TEST_RESONANCES_help)
        self.gcode.register_command("SHAPER_CALIBRATE",
                                    self.cmd_SHAPER_CALIBRATE,
                                    desc=self.cmd_SHAPER_CALIBRATE_help)
        self.printer.register_event_handler("klippy:connect", self.connect)

    def connect(self):
        self.accel_chips = [
                (chip_axis, self.printer.lookup_object(chip_name))
                for chip_axis, chip_name in self.accel_chip_names]

    def _calibrate_chips(self, axes, gcmd):
        calibrate_chips = []
        for chip_axis, chip in self.accel_chips:
            for axis in axes:
                if axis.matches(chip_axis):
                    calibrate_chips.append(chip)
                    break
        for chip in calibrate_chips:
            gcmd.respond_info('Autocalibrating %s' % (
                chip.get_config().get_name(),))
            adxl345.AccelerometerCalibrator(self.printer, chip).calibrate(gcmd)

    def _run_test(self, gcmd, axes, helper, raw_name_suffix=None):
        toolhead = self.printer.lookup_object('toolhead')
        calibration_data = {axis: None for axis in axes}

        self.test.prepare_test(gcmd)
        test_points = self.test.get_start_test_points()
        for ptx_ind, point in enumerate(test_points):
            toolhead.manual_move(point, self.move_speed)
            if self.autocalibrate and ptx_ind == 0:
                self._calibrate_chips(axes, gcmd)
            if len(test_points) > 1:
                gcmd.respond_info(
                        "Probing point (%.3f, %.3f, %.3f)" % tuple(point))
            for axis in axes:
                toolhead.wait_moves()
                toolhead.dwell(0.500)
                if len(axes) > 1:
                    gcmd.respond_info("Testing axis %s" % axis.get_name())
                input_shaper = self.printer.lookup_object('input_shaper', None)
                # Disable input shaping as appropriate
                if input_shaper is not None and not gcmd.get_int(
                        'INPUT_SHAPING', 0):
                    input_shaper.disable_shaping()
                    gcmd.respond_info(
                            "Disabled [input_shaper] for resonance testing")
                else:
                    input_shaper = None
                try:
                    # Start acceleration measurements
                    raw_values = []
                    for chip_axis, chip in self.accel_chips:
                        if axis.matches(chip_axis):
                            aclient = chip.start_internal_client()
                            raw_values.append((chip_axis, aclient))
                    # Generate moves
                    self.test.run_test(axis, gcmd)
                    # Obtain the measurement results
                    for chip_axis, aclient in raw_values:
                        aclient.finish_measurements()
                        if raw_name_suffix is not None:
                            raw_name = self.get_filename(
                                    'raw_data', raw_name_suffix, axis,
                                    point if len(test_points) > 1 else None)
                            aclient.write_to_file(raw_name)
                            gcmd.respond_info(
                                    "Writing raw accelerometer data to "
                                    "%s file" % (raw_name,))
                finally:
                    # Restore input shaper if it was disabled
                    # for resonance testing
                    if input_shaper is not None:
                        input_shaper.enable_shaping()
                        gcmd.respond_info("Re-enabled [input_shaper]")
                if helper is None:
                    continue
                for chip_axis, aclient in raw_values:
                    if not aclient.has_valid_samples():
                        raise gcmd.error(
                                "%s-axis accelerometer measured no data" % (
                                    chip_axis,))
                    new_data = helper.process_accelerometer_data(aclient)
                    if calibration_data[axis] is None:
                        calibration_data[axis] = new_data
                    else:
                        calibration_data[axis].add_data(new_data)
        return calibration_data
    cmd_TEST_RESONANCES_help = ("Runs the resonance test for a specifed axis")
    def cmd_TEST_RESONANCES(self, gcmd):
        # Parse parameters
        axis = _parse_axis(gcmd, gcmd.get("AXIS").lower())

        outputs = gcmd.get("OUTPUT", "resonances").lower().split(',')
        for output in outputs:
            if output not in ['resonances', 'raw_data']:
                raise gcmd.error("Unsupported output '%s', only 'resonances'"
                                 " and 'raw_data' are supported" % (output,))
        if not outputs:
            raise gcmd.error("No output specified, at least one of 'resonances'"
                             " or 'raw_data' must be set in OUTPUT parameter")
        name_suffix = gcmd.get("NAME", time.strftime("%Y%m%d_%H%M%S"))
        if not self.is_valid_name_suffix(name_suffix):
            raise gcmd.error("Invalid NAME parameter")
        csv_output = 'resonances' in outputs
        raw_output = 'raw_data' in outputs

        # Setup calculation of resonances
        if csv_output:
            helper = shaper_calibrate.ShaperCalibrate(self.printer)
        else:
            helper = None

        data = self._run_test(
                gcmd, [axis], helper,
                raw_name_suffix=name_suffix if raw_output else None)[axis]
        if csv_output:
            csv_name = self.save_calibration_data('resonances', name_suffix,
                                                  helper, axis, data)
            gcmd.respond_info(
                    "Resonances data written to %s file" % (csv_name,))
    cmd_SHAPER_CALIBRATE_help = (
        "Simular to TEST_RESONANCES but suggest input shaper config")
    def cmd_SHAPER_CALIBRATE(self, gcmd):
        # Parse parameters
        axis = gcmd.get("AXIS", None)
        if not axis:
            calibrate_axes = [TestAxis('x'), TestAxis('y')]
        elif axis.lower() not in 'xy':
            raise gcmd.error("Unsupported axis '%s'" % (axis,))
        else:
            calibrate_axes = [TestAxis(axis.lower())]

        max_smoothing = gcmd.get_float(
                "MAX_SMOOTHING", self.max_smoothing, minval=0.05)

        name_suffix = gcmd.get("NAME", time.strftime("%Y%m%d_%H%M%S"))
        if not self.is_valid_name_suffix(name_suffix):
            raise gcmd.error("Invalid NAME parameter")

        # Setup shaper calibration
        helper = shaper_calibrate.ShaperCalibrate(self.printer)

        calibration_data = self._run_test(gcmd, calibrate_axes, helper)

        configfile = self.printer.lookup_object('configfile')
        for axis in calibrate_axes:
            axis_name = axis.get_name()
            gcmd.respond_info(
                    "Calculating the best input shaper parameters for %s axis"
                    % (axis_name,))
            calibration_data[axis].normalize_to_frequencies()
            best_shaper, all_shapers = helper.find_best_shaper(
                    calibration_data[axis], max_smoothing, gcmd.respond_info)
            gcmd.respond_info(
                    "Recommended shaper_type_%s = %s, shaper_freq_%s = %.1f Hz"
                    % (axis_name, best_shaper.name,
                       axis_name, best_shaper.freq))
            helper.save_params(configfile, axis_name,
                               best_shaper.name, best_shaper.freq)
            csv_name = self.save_calibration_data(
                    'calibration_data', name_suffix, helper, axis,
                    calibration_data[axis], all_shapers)
            gcmd.respond_info(
                    "Shaper calibration data written to %s file" % (csv_name,))
        gcmd.respond_info(
            "The SAVE_CONFIG command will update the printer config file\n"
            "with these parameters and restart the printer.")
    cmd_MEASURE_AXES_NOISE_help = (
        "Measures noise of all enabled accelerometer chips")
    def cmd_MEASURE_AXES_NOISE(self, gcmd):
        meas_time = gcmd.get_float("MEAS_TIME", 2.)
        raw_values = [(chip_axis, chip.start_internal_client())
                      for chip_axis, chip in self.accel_chips]
        self.printer.lookup_object('toolhead').dwell(meas_time)
        for chip_axis, aclient in raw_values:
            aclient.finish_measurements()
        helper = shaper_calibrate.ShaperCalibrate(self.printer)
        for chip_axis, aclient in raw_values:
            if not aclient.has_valid_samples():
                raise gcmd.error(
                        "%s-axis accelerometer measured no data" % (chip_axis,))
            data = helper.process_accelerometer_data(aclient)
            vx = data.psd_x.mean()
            vy = data.psd_y.mean()
            vz = data.psd_z.mean()
            gcmd.respond_info("Axes noise for %s-axis accelerometer: "
                              "%.6f (x), %.6f (y), %.6f (z)" % (
                                  chip_axis, vx, vy, vz))

    def is_valid_name_suffix(self, name_suffix):
        return name_suffix.replace('-', '').replace('_', '').isalnum()

    def get_filename(self, base, name_suffix, axis=None, point=None):
        name = base
        if axis:
            name += '_' + axis.get_name()
        if point:
            name += "_%.3f_%.3f_%.3f" % (point[0], point[1], point[2])
        name += '_' + name_suffix
        return os.path.join("/tmp", name + ".csv")

    def save_calibration_data(self, base_name, name_suffix, shaper_calibrate,
                              axis, calibration_data, all_shapers=None):
        output = self.get_filename(base_name, name_suffix, axis)
        shaper_calibrate.save_calibration_data(output, calibration_data,
                                               all_shapers)
        return output

def load_config(config):
    return ResonanceTester(config)
