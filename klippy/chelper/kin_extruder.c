// Extruder stepper pulse time generation
//
// Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stddef.h> // offsetof
#include <stdlib.h> // malloc
#include <string.h> // memset
#include "compiler.h" // __visible
#include "itersolve.h" // struct stepper_kinematics
#include "kin_shaper.h" // struct shaper_pulses
#include "pyhelper.h" // errorf
#include "trapq.h" // move_get_distance

// Without pressure advance, the extruder stepper position is:
//     extruder_position(t) = nominal_position(t)
// When pressure advance is enabled, additional filament is pushed
// into the extruder during acceleration (and retracted during
// deceleration). The formula is:
//     pa_position(t) = (nominal_position(t)
//                       + pressure_advance * nominal_velocity(t))
// Which is then "smoothed" using a weighted average:
//     smooth_position(t) = (
//         definitive_integral(pa_position(x) * (smooth_time/2 - abs(t-x)) * dx,
//                             from=t-smooth_time/2, to=t+smooth_time/2)
//         / ((smooth_time/2)**2))

// Calculate the definitive integral of the motion formula:
//   position(t) = base + t * (start_v + t * half_accel)
static double
extruder_integrate(double base, double start_v, double half_accel
                   , double start, double end)
{
    double half_v = .5 * start_v, sixth_a = (1. / 3.) * half_accel;
    double si = start * (base + start * (half_v + start * sixth_a));
    double ei = end * (base + end * (half_v + end * sixth_a));
    return ei - si;
}

// Calculate the definitive integral of time weighted position:
//   weighted_position(t) = t * (base + t * (start_v + t * half_accel))
static double
extruder_integrate_time(double base, double start_v, double half_accel
                        , double start, double end)
{
    double half_b = .5 * base, third_v = (1. / 3.) * start_v;
    double eighth_a = .25 * half_accel;
    double si = start * start * (half_b + start * (third_v + start * eighth_a));
    double ei = end * end * (half_b + end * (third_v + end * eighth_a));
    return ei - si;
}

// Calculate the definitive integral of extruder for a given move
static void
pa_move_integrate(struct move *m, int axis
                  , double base, double start, double end, double time_offset
                  , double *pos_integral, double *pa_velocity_integral)
{
    if (start < 0.)
        start = 0.;
    if (end > m->move_t)
        end = m->move_t;
    // Calculate base position and velocity with pressure advance
    int can_pressure_advance = m->axes_r.x > 0. || m->axes_r.y > 0.;
    double axis_r = m->axes_r.axis[axis - 'x'];
    double start_v = m->start_v * axis_r;
    double ha = m->half_accel * axis_r;
    // Calculate definitive integral
    double iext = extruder_integrate(base, start_v, ha, start, end);
    double wgt_ext = extruder_integrate_time(base, start_v, ha, start, end);
    *pos_integral = wgt_ext - time_offset * iext;
    if (!can_pressure_advance) {
        *pa_velocity_integral = 0.;
    } else {
        double ivel = extruder_integrate(start_v, 2. * ha, 0., start, end);
        double wgt_vel = extruder_integrate(0., start_v, 2. * ha, start, end);
        *pa_velocity_integral = wgt_vel - time_offset * ivel;
    }
}

// Calculate the definitive integral of the extruder over a range of moves
static void
pa_range_integrate(struct move *m, int axis, double move_time, double hst
                   , double *pos_integral, double *pa_velocity_integral)
{
    while (unlikely(move_time < 0.)) {
        m = list_prev_entry(m, node);
        move_time += m->move_t;
    }
    while (unlikely(move_time > m->move_t)) {
        move_time -= m->move_t;
        m = list_next_entry(m, node);
    }
    // Calculate integral for the current move
    *pos_integral = *pa_velocity_integral = 0.;
    double m_pos_int, m_pa_vel_int;
    double start = move_time - hst, end = move_time + hst;
    double start_base = m->start_pos.axis[axis - 'x'];
    pa_move_integrate(m, axis, 0., start, move_time, start,
                      &m_pos_int, &m_pa_vel_int);
    *pos_integral += m_pos_int;
    *pa_velocity_integral += m_pa_vel_int;
    pa_move_integrate(m, axis, 0., move_time, end, end,
                      &m_pos_int, &m_pa_vel_int);
    *pos_integral -= m_pos_int;
    *pa_velocity_integral -= m_pa_vel_int;
    // Integrate over previous moves
    struct move *prev = m;
    while (unlikely(start < 0.)) {
        prev = list_prev_entry(prev, node);
        start += prev->move_t;
        double base = prev->start_pos.axis[axis - 'x'] - start_base;
        pa_move_integrate(prev, axis, base, start, prev->move_t, start,
                          &m_pos_int, &m_pa_vel_int);
        *pos_integral += m_pos_int;
        *pa_velocity_integral += m_pa_vel_int;
    }
    // Integrate over future moves
    while (unlikely(end > m->move_t)) {
        end -= m->move_t;
        m = list_next_entry(m, node);
        double base = m->start_pos.axis[axis - 'x'] - start_base;
        pa_move_integrate(m, axis, base, 0., end, end,
                          &m_pos_int, &m_pa_vel_int);
        *pos_integral -= m_pos_int;
        *pa_velocity_integral -= m_pa_vel_int;
    }
    *pos_integral += start_base * hst * hst;
}

static void
shaper_pa_range_integrate(struct move *m, int axis, double move_time
                          , double hst, struct shaper_pulses *sp
                          , double *pos_integral, double *pa_velocity_integral)
{
    *pos_integral = *pa_velocity_integral = 0.;
    double p_pos_int, p_pa_vel_int;
    int num_pulses = sp->num_pulses, i;
    for (i = 0; i < num_pulses; ++i) {
        double t = sp->pulses[i].t, a = sp->pulses[i].a;
        pa_range_integrate(m, axis, move_time + t, hst,
                           &p_pos_int, &p_pa_vel_int);
        *pos_integral += a * p_pos_int;
        *pa_velocity_integral += a * p_pa_vel_int;
    }
}

struct extruder_stepper {
    struct stepper_kinematics sk;
    struct shaper_pulses sp[3];
    double pressure_advance, half_smooth_time, inv_half_smooth_time2;
};

static double
extruder_calc_position(struct stepper_kinematics *sk, struct move *m
                       , double move_time)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    double hst = es->half_smooth_time;
    int i;
    struct coord e_pos, pa_vel;
    double move_dist = move_get_distance(m, move_time);
    for (i = 0; i < 3; ++i) {
        int axis = 'x' + i;
        struct shaper_pulses* sp = &es->sp[i];
        int num_pulses = sp->num_pulses;
        if (!hst) {
            e_pos.axis[i] = num_pulses
                ? shaper_calc_position(m, axis, move_time, sp)
                : m->start_pos.axis[i] + m->axes_r.axis[i] * move_dist;
            pa_vel.axis[i] = 0.;
        } else {
            if (num_pulses) {
                shaper_pa_range_integrate(m, axis, move_time, hst, sp,
                                          &e_pos.axis[i], &pa_vel.axis[i]);
            } else {
                pa_range_integrate(m, axis, move_time, hst,
                                   &e_pos.axis[i], &pa_vel.axis[i]);
            }
            e_pos.axis[i] *= es->inv_half_smooth_time2;
            pa_vel.axis[i] *= es->inv_half_smooth_time2;
        }
    }
    double position = e_pos.x + e_pos.y + e_pos.z;
    double pa_velocity = pa_vel.x + pa_vel.y + pa_vel.z;
    if (hst) {
        position += es->pressure_advance * pa_velocity;
    }
    return position;
}

static void
extruder_note_generation_time(struct extruder_stepper *es)
{
    double pre_active = 0., post_active = 0.;
    int i;
    for (i = 0; i < 2; ++i) {
        struct shaper_pulses* sp = &es->sp[i];
        if (!es->sp[i].num_pulses) continue;
        pre_active = sp->pulses[sp->num_pulses-1].t > pre_active
            ? sp->pulses[sp->num_pulses-1].t : pre_active;
        post_active = -sp->pulses[0].t > post_active
            ? -sp->pulses[0].t : post_active;
    }
    if (es->half_smooth_time) {
        pre_active += es->half_smooth_time;
        post_active += es->half_smooth_time;
    }
    es->sk.gen_steps_pre_active = pre_active;
    es->sk.gen_steps_post_active = post_active;
}

void __visible
extruder_set_pressure_advance(struct stepper_kinematics *sk
                              , double pressure_advance, double smooth_time)
{
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    double hst = smooth_time * .5;
    es->half_smooth_time = hst;
    extruder_note_generation_time(es);
    if (! hst) {
        es->pressure_advance = 0.;
        return;
    }
    es->inv_half_smooth_time2 = 1. / (hst * hst);
    es->pressure_advance = pressure_advance;
}

int __visible
extruder_set_shaper_params(struct stepper_kinematics *sk, char axis
                           , int n, double a[], double t[])
{
    if (axis != 'x' && axis != 'y')
        return -1;
    struct extruder_stepper *es = container_of(sk, struct extruder_stepper, sk);
    struct shaper_pulses *sp = &es->sp[axis-'x'];
    int status = init_shaper(n, a, t, sp);
    extruder_note_generation_time(es);
    return status;
}

struct stepper_kinematics * __visible
extruder_stepper_alloc(void)
{
    struct extruder_stepper *es = malloc(sizeof(*es));
    memset(es, 0, sizeof(*es));
    es->sk.calc_position_cb = extruder_calc_position;
    es->sk.active_flags = AF_X | AF_Y | AF_Z;
    return &es->sk;
}
