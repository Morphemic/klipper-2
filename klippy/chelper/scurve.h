#ifndef SCURVE_H
#define SCURVE_H

struct scurve {
    double c0, c1, c2, c3, c4, c5, c6;
    double offset_t;
};

// Find the distance travel on an S-Curve
static inline double
scurve_eval(struct scurve *s, double time)
{
    time += s->offset_t;
    double v = s->c6;
    v = s->c5 + v * time;
    v = s->c4 + v * time;
    v = s->c3 + v * time;
    v = s->c2 + v * time;
    v = s->c1 + v * time;
    return v * time + s->c0;
}

void scurve_fill(struct scurve *s, int accel_order
        , double accel_t, double accel_offset_t, double total_accel_t
        , double start_accel_v, double effective_accel, double accel_comp);
double scurve_get_time(struct scurve *s, double max_scurve_t, double distance);

#endif // scurve.h
