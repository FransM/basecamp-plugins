## DisplayPad Clock

Shows the current time on a DisplayPad button.

The appearance of the clock is configured through the **Action value** of the
key it's bound to. Possible flags:

| Flag   | Effect                                       |
|--------|----------------------------------------------|
| `sec`  | show seconds                                 |
| `12h`  | use a 12-hour AM/PM clock (default is 24h)   |
| `date` | show the date below the time                 |

Flags can be combined with commas, e.g. `sec,date` or `12h,sec`.

If the action value is left empty, you get a plain 24-hour clock with hours
and minutes.

### Stopwatch

Pressing the assigned key starts a stopwatch on the same button. The display
updates roughly every 100 ms. Pressing the key again stops the timer — the
final time stays visible for 5 seconds, then the regular clock returns.
