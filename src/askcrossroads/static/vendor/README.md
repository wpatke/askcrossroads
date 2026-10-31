# Vendored chart libraries

The page draws charts with [Vega-Lite](https://vega.github.io/vega-lite/), which runs on
[Vega](https://vega.github.io/vega/). Both are kept here, unmodified, so that charts work with no
internet connection and the page contacts nothing outside this machine.

| File | Package | Version | Downloaded from | SHA-256 |
|---|---|---|---|---|
| `vega.min.js` | vega | 6.4.0 | `https://cdn.jsdelivr.net/npm/vega@6.4.0/build/vega.min.js` | `8f6a3587cf8d4f42c7e08120e3eb05d067e746d554e39d2dcf52acc0bd5ba28f` |
| `vega-lite.min.js` | vega-lite | 6.5.0 | `https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/build/vega-lite.min.js` | `83e215e84e308ee8688fa6503ba0f1cf8a3c05c0d61df2841a0b92bf86809e00` |
| `vega-LICENSE.txt` | vega | 6.4.0 | `https://cdn.jsdelivr.net/npm/vega@6.4.0/LICENSE` | `63727832aaf62004a2b249c933e327f3c90caf49e41a72f4bf436edf632cbda8` |
| `vega-lite-LICENSE.txt` | vega-lite | 6.5.0 | `https://cdn.jsdelivr.net/npm/vega-lite@6.5.0/LICENSE` | `f618900fd0d64046963b29f40590cdd1e341a2f41449f99110d82fd81fea808c` |

Both libraries are BSD-3-Clause licensed by the University of Washington Interactive Data Lab. Their
licence texts are kept beside them, as that licence requires.

`vega-lite.min.js` ends with a `sourceMappingURL` comment. The map file is not included. Browsers
only ask for it when developer tools are open, and nothing breaks without it.

## Updating

1. Choose a Vega-Lite release, then the Vega release that its `package.json` `peerDependencies`
   asks for.
2. Download the four files from the URLs above, with the new version numbers.
3. Update this table and `VENDORED` in `tests/test_page.py` with the new SHA-256 values
   (`shasum -a 256 *`).
4. Update the versions in `tests/test_browser.py::test_the_chart_libraries_are_the_pinned_versions`.
