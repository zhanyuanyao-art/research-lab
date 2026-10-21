# PyInstaller spec for Research Lab.app (run through packaging/macos/build.sh).
import os
import re

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
VERSION = re.search(r'^version = "([^"]+)"', open(os.path.join(ROOT, "pyproject.toml")).read(), re.M).group(1)
# build.sh --worktree builds "Research Lab Dev" with its own bundle id, so a development build never
# poses as the installed app in Launchpad, Spotlight or "Open With".
APP_NAME = os.environ.get("RL_APP_NAME", "Research Lab")
BUNDLE_ID = os.environ.get("RL_BUNDLE_ID", "io.github.leoyao0701.researchlab")

# Routes, CLI sub-apps and AI jobs are imported by name at runtime, so every submodule is listed.
hidden = (collect_submodules("research_lab") + collect_submodules("uvicorn")
          + ["webview.platforms.cocoa", "multipart", "python_multipart"])
trial = os.environ.get("RL_DISTRIBUTION") == "trial"
maintainer = ["research_lab.devflow", "research_lab.cli.dev", "research_lab.cli.launchd",
              "research_lab.web.routes.dev", "research_lab.web.routes.digest", "research_lab.digest"] if trial else []
hidden = [name for name in hidden if not any(name == m or name.startswith(m + ".") for m in maintainer)]
datas = collect_data_files("research_lab", includes=["web/templates/**/*", "web/static/**/*", "db/migrations/*.sql", "agent/skills/*.md", "replication/methods/*.md"])

if trial:
    datas.append((os.path.join(ROOT, "packaging/macos/build/licenses"), "licenses"))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[os.path.join(ROOT, "app")],
    hiddenimports=hidden,
    datas=datas,
    excludes=maintainer + ["tkinter", "IPython", "jupyter", "notebook", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "sphinx"],
    noarchive=False,
)
if trial:
    import json
    with open(os.path.join(ROOT, "packaging/macos/build/licenses/bundle-modules.json"), "w") as f:
        json.dump({"python": [x[0] for x in a.pure], "native": [x[0] for x in a.binaries]}, f, indent=2)
    a.datas += [("licenses/bundle-modules.json", os.path.join(ROOT, "packaging/macos/build/licenses/bundle-modules.json"), "DATA")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Research Lab", console=False,
          argv_emulation=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Research Lab")
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=os.path.join(SPECPATH, "build", "icon.icns"),
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "14.0" if trial else "13.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSRequiresAquaSystemAppearance": False,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHumanReadableCopyright": "Leo Yao",
    },
)
