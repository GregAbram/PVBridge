## The person's own filters

These are loaded in ParaView and used like any filter, with `pvl_derive(input, "<FilterName>", name=..., **props)`. To see any loaded filter's description and properties: `pvl_filter_help("<FilterName>")`; to list loaded filters: `pvl_filters("<part of a name>")`.

### Molar Point Sampler (`MolarPointSampler`)

Turns a concentration field into a point cloud in which every point stands for the same amount of material, q, so point density shows concentration. Use it when the person asks to show a concentration (smoke, a tracer, a chemical) "as points", "as particles", "as a point cloud", or "sampled", or names the molar sampler.

- `Concentration=["POINTS", var]` or `["CELLS", var]`: the concentration variable (amount per m^3 on volumes, per m^2 on surfaces).
- `AmountPerPoint=q`: the amount each point represents. Expected points = total amount / q. Choose q for about 50,000 points unless the person asks for a number of points or a q: `q = pvl_integrate(name, var) / 50000`. Say how many points that gives ("about 50 thousand points"). If they say "more points" or "fewer points", scale q down or up.
- `LengthScale`: metres per mesh unit, if the person says the mesh isn't in metres (it changes the total, so recompute q).
- `PointDataPlacement`: 0 Linear (default, follows the field inside cells), 1 Uniform Per Cell (points stay still between timesteps). Use 1 if they want points that don't jump around when time changes.
- `Seed`: a different random draw, same number of points.
- `PassArrays=1` (default) carries the input's other variables onto the points, so the points can be coloured by them (e.g. the concentration itself).
- `MaxPoints` (default 5,000,000) is a safety limit; leave it.

The output is points, which the headset shows as points. Colour them by the concentration variable unless asked otherwise. Example: "show the smoke as points" on an object named "plume" →
```
q = pvl_integrate("plume", "smoke") / 50000
pvl_derive("plume", "MolarPointSampler", name="plume smoke points", Concentration=["POINTS", "smoke"], AmountPerPoint=q)
```
Data with timesteps is resampled at each time, so the points follow the field as time changes.
