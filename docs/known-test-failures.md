# Known test failures (pre-existing)

Five tests fail on the repository **before** any EduMix work (verified on tag
`checkpoint-pre-edumix-20261104`, and unchanged afterwards). None is caused by EduMix and none was
"fixed" here - this file only records the root causes so they can be dealt with as a separate change.

| # | Test | Root cause | Kind |
|---|------|------------|------|
| 1 | `test_sources_zoom.py::test_public_view_masks_everything` | Test passes `passcode="447722"` together with a link that already carries `pwd=`. Since commit `6563bba` ("Fix false 'passcode contains invalid characters' error on /zoom") `_validate_zoom_options` treats the link's own `pwd=` as authoritative and **drops** a separate passcode (`sources.py`, comment "The link's own pwd= is authoritative"). The test's expectation (`options["passcode"] == "447722"`) predates that. | Stale test vs. intentional behaviour |
| 2 | `test_sources_zoom.py::test_edit_from_public_view_keeps_secrets` | Same cause: the passcode the test seeds is dropped because `LINK` has `pwd=AbC123xyz`. | Stale test |
| 3 | `test_sources_zoom.py::test_reveal_and_rev` | Same cause: `reveal_secrets()` returns `passcode: ""` instead of `"1"` for a `pwd=` link. | Stale test |
| 4 | `test_sources_zoom.py::test_zoom_option_validation[opts2-Passcode may only]` | Same cause: an invalid passcode (`"has space"`) on a `pwd=` link is now silently dropped rather than rejected, so `ValidationError` is never raised. The case needs a link *without* `pwd=` to exercise the passcode check. | Stale test |
| 5 | `test_vnc.py::test_vnc_page_authenticated` | Asserts the `/vnc` page contains `vnc-config`. Commit `4ba8592` ("Stop embedding the real VNC password in /vnc's page source") deliberately removed that JSON blob from `templates/vnc.html` (the server-side VNC-auth proxy now authenticates, so the page needs no config/secret). The test was not updated. | Stale test vs. deliberate security fix |

## What a fix would look like (not applied)

* 1-4: give those tests a link without `pwd=` (e.g. `https://us06web.zoom.us/j/81234567890`) when they
  mean to exercise the separate passcode field, and add one test that documents "link `pwd=` wins".
* 5: drop the `vnc-config` assertion (assert the page contains no VNC password instead).

Neither needs product-code changes. Confirm with `git log -S` on the two commits above.
