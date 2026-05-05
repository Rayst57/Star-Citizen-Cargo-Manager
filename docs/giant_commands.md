# "Giant" Loadmaster — Voice Command List

All commands start with the wake phrase **"Hey Giant"**. The app listens
continuously but only acts on speech that begins with the wake phrase.

A **push-to-talk bind** (configured in Settings → Voice Input) can also be
used to skip the wake phrase for a single utterance. PTT supports keyboard,
mouse, gamepad, and HOTAS/joystick inputs. See `settings.md`.

After the wake phrase fires, the app captures the rest of the utterance and
routes it to the LLM. The LLM picks a tool from the list below and executes
it. Natural-language phrasing is fine — examples are just one way to say each
command.

---

## Trip / Session

| Intent | Example phrasing | Tool call |
|---|---|---|
| Start a shared trip | "Hey Giant, start a trip" | `start_trip` |
| Join a shared trip | "Hey Giant, join trip Golf Romeo X-ray seven kilo nine" | `join_trip` |
| Read out invite code | "Hey Giant, what's my code?" | `get_invite_code` |
| List crew | "Hey Giant, who's on the crew?" | `list_crew` |
| Promote crew member | "Hey Giant, give Pyro edit rights" *(host only)* | `promote_member` |
| End the trip | "Hey Giant, end the trip" *(host only)* | `end_trip` |
| Leave the trip | "Hey Giant, drop off the crew" | `leave_trip` |

## Contract Management

| Intent | Example phrasing | Tool call |
|---|---|---|
| Add a contract | "Hey Giant, new contract from Seraphim, 5 SCU processed food to Ambitious Dream" | `add_contract` |
| Remove a contract | "Hey Giant, drop contract three" / "remove the Seraphim run" | `remove_contract` |
| Edit a contract | "Hey Giant, change contract two to 8 SCU" | `edit_contract` |
| List all contracts | "Hey Giant, what contracts do I have?" | `list_contracts` |
| Clear everything | "Hey Giant, scrap the manifest" | `clear_contracts` |
| Undo last change | "Hey Giant, scratch that" / "undo" | `undo` |

## Route

| Intent | Example phrasing | Tool call |
|---|---|---|
| Show the route | "Hey Giant, what's the route?" | `get_route` |
| Re-plan now | "Hey Giant, re-plan" / "optimize again" | `replan` |
| Next stop | "Hey Giant, what's next?" / "next stop" | `get_next_stop` |
| Mark stop complete | "Hey Giant, stop complete" / "we're loaded out" | `complete_current_stop` |
| Skip a stop | "Hey Giant, skip Ambitious Dream" | `skip_stop` |

## Cargo / Loading

| Intent | Example phrasing | Tool call |
|---|---|---|
| Show load plan | "Hey Giant, show the load" | `get_load_plan` |
| Read out a zone | "Hey Giant, what's in R1?" | `get_zone_contents` |
| Capacity check | "Hey Giant, how full are we?" | `get_capacity` |
| Explain a placement | "Hey Giant, why is that in F2?" | `explain_placement` |

## Ship

| Intent | Example phrasing | Tool call |
|---|---|---|
| Change active ship | "Hey Giant, switch to Caterpillar" *(future)* | `set_ship` |
| Show ship info | "Hey Giant, ship status" | `get_ship_info` |

## Session

| Intent | Example phrasing | Tool call |
|---|---|---|
| Save current plan | "Hey Giant, save this run as 'Hurston loop'" | `save_session` |
| Load a saved plan | "Hey Giant, load Hurston loop" | `load_session` |
| Export | "Hey Giant, export to PDF" | `export_plan` |

## Conversational / Help

| Intent | Example phrasing | Tool call |
|---|---|---|
| Repeat last response | "Hey Giant, say again" | (TTS replay) |
| Pause listening | "Hey Giant, stand by" | `pause_listening` |
| Resume listening | "Hey Giant, back on" *(spoken before pause ends)* | `resume_listening` |
| Help | "Hey Giant, what can you do?" | `list_capabilities` |

---

## Reserved phrases (no wake word required)

A small set of utility phrases work without the wake word, because they are
either safety-critical or always-on by design:

- **"Stand by, Giant"** — instantly mute mic and pause any in-progress TTS
- **"Cancel that"** — abort the command currently being processed

---

## Future / ideas

- "Hey Giant, plot me the most profitable route" *(profit optimization)*
- "Hey Giant, how long is this run?" *(fuel/time estimation)*
- "Hey Giant, split this between two ships" *(multi-ship)*
- "Hey Giant, prices at Area 18" *(live SC market data)*
