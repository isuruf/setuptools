import os
import sys
from configparser import ConfigParser
from itertools import product
from inspect import cleandoc

from setuptools.command.sdist import sdist
from setuptools.dist import Distribution
from setuptools.discovery import find_package_path, find_parent_package
from setuptools.errors import PackageDiscoveryError

import setuptools  # noqa -- force distutils.core to be patched
import distutils.core

import pytest
from path import Path as _Path

from .contexts import quiet
from .integration.helpers import get_sdist_members, get_wheel_members, run
from .textwrap import DALS


def test_find_parent_package(tmp_path):
    # find_parent_package should find a non-namespace parent package
    (tmp_path / "src/namespace/pkg/nested").mkdir(exist_ok=True, parents=True)
    (tmp_path / "src/namespace/pkg/nested/__init__.py").touch()
    (tmp_path / "src/namespace/pkg/__init__.py").touch()
    packages = ["namespace", "namespace.pkg", "namespace.pkg.nested"]
    assert find_parent_package(packages, {"": "src"}, tmp_path) == "namespace.pkg"


def test_find_parent_package_multiple_toplevel(tmp_path):
    # find_parent_package should return null if the given list of packages does not
    # have a single parent package
    multiple = ["pkg", "pkg1", "pkg2"]
    for name in multiple:
        (tmp_path / f"src/{name}").mkdir(exist_ok=True, parents=True)
        (tmp_path / f"src/{name}/__init__.py").touch()
    assert find_parent_package(multiple, {"": "src"}, tmp_path) is None


class TestDiscoverPackagesAndPyModules:
    """Make sure discovered values for ``packages`` and ``py_modules`` work
    similarly to explicit configuration for the simple scenarios.
    """
    OPTIONS = {
        # Different options according to the circumstance being tested
        "explicit-src": {
            "package_dir": {"": "src"},
            "packages": ["pkg"]
        },
        "variation-lib": {
            "package_dir": {"": "lib"},  # variation of the source-layout
        },
        "explicit-flat": {
            "packages": ["pkg"]
        },
        "explicit-single_module": {
            "py_modules": ["pkg"]
        },
        "explicit-namespace": {
            "packages": ["ns", "ns.pkg"]
        },
        "automatic-src": {},
        "automatic-flat": {},
        "automatic-single_module": {},
        "automatic-namespace": {}
    }
    FILES = {
        "src": ["src/pkg/__init__.py", "src/pkg/main.py"],
        "lib": ["lib/pkg/__init__.py", "lib/pkg/main.py"],
        "flat": ["pkg/__init__.py", "pkg/main.py"],
        "single_module": ["pkg.py"],
        "namespace": ["ns/pkg/__init__.py"]
    }

    def _get_info(self, circumstance):
        _, _, layout = circumstance.partition("-")
        files = self.FILES[layout]
        options = self.OPTIONS[circumstance]
        return files, options

    @pytest.mark.parametrize("circumstance", OPTIONS.keys())
    def test_sdist_filelist(self, tmp_path, circumstance):
        files, options = self._get_info(circumstance)
        _populate_project_dir(tmp_path, files, options)

        _, cmd = _run_sdist_programatically(tmp_path, options)

        manifest = [f.replace(os.sep, "/") for f in cmd.filelist.files]
        for file in files:
            assert any(f.endswith(file) for f in manifest)

    @pytest.mark.parametrize("circumstance", OPTIONS.keys())
    def test_project(self, tmp_path, circumstance):
        files, options = self._get_info(circumstance)
        _populate_project_dir(tmp_path, files, options)

        # Simulate a pre-existing `build` directory
        (tmp_path / "build").mkdir()
        (tmp_path / "build/lib").mkdir()
        (tmp_path / "build/bdist.linux-x86_64").mkdir()
        (tmp_path / "build/bdist.linux-x86_64/file.py").touch()
        (tmp_path / "build/lib/__init__.py").touch()
        (tmp_path / "build/lib/file.py").touch()
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist/file.py").touch()

        _run_build(tmp_path)

        sdist_files = get_sdist_members(next(tmp_path.glob("dist/*.tar.gz")))
        print("~~~~~ sdist_members ~~~~~")
        print('\n'.join(sdist_files))
        assert sdist_files >= set(files)

        wheel_files = get_wheel_members(next(tmp_path.glob("dist/*.whl")))
        print("~~~~~ wheel_members ~~~~~")
        print('\n'.join(wheel_files))
        orig_files = {f.replace("src/", "").replace("lib/", "") for f in files}
        assert wheel_files >= orig_files

        # Make sure build files are not included by mistake
        for file in wheel_files:
            assert "build" not in files
            assert "dist" not in files

    PURPOSEFULLY_EMPY = {
        "setup.cfg": DALS(
            """
            [metadata]
            name = myproj
            version = 0.0.0

            [options]
            {param} =
            """
        ),
        "setup.py": DALS(
            """
            __import__('setuptools').setup(
                name="myproj",
                version="0.0.0",
                {param}=[]
            )
            """
        ),
        "pyproject.toml": DALS(
            """
            [build-system]
            requires = []
            build-backend = 'setuptools.build_meta'

            [project]
            name = "myproj"
            version = "0.0.0"

            [tool.setuptools]
            {param} = []
            """
        ),
        "template-pyproject.toml": DALS(
            """
            [build-system]
            requires = []
            build-backend = 'setuptools.build_meta'
            """
        )
    }

    @pytest.mark.parametrize(
        "config_file, param, circumstance",
        product(
            ["setup.cfg", "setup.py", "pyproject.toml"],
            ["packages", "py_modules"],
            FILES.keys()
        )
    )
    def test_purposefully_empty(self, tmp_path, config_file, param, circumstance):
        files = self.FILES[circumstance] + ["mod.py", "other.py", "src/pkg/__init__.py"]
        _populate_project_dir(tmp_path, files, {})

        if config_file == "pyproject.toml":
            template_param = param.replace("_", "-")
        else:
            # Make sure build works with or without setup.cfg
            pyproject = self.PURPOSEFULLY_EMPY["template-pyproject.toml"]
            (tmp_path / "pyproject.toml").write_text(pyproject)
            template_param = param

        config = self.PURPOSEFULLY_EMPY[config_file].format(param=template_param)
        (tmp_path / config_file).write_text(config)

        dist = _get_dist(tmp_path, {})
        # When either parameter package or py_modules is an empty list,
        # then there should be no discovery
        assert getattr(dist, param) == []
        other = {"py_modules": "packages", "packages": "py_modules"}[param]
        assert getattr(dist, other) is None

    @pytest.mark.parametrize(
        "extra_files, pkgs",
        [
            (["venv/bin/simulate_venv"], {"pkg"}),
            (["pkg-stubs/__init__.pyi"], {"pkg", "pkg-stubs"}),
            (["other-stubs/__init__.pyi"], {"pkg", "other-stubs"}),
            (
                # Type stubs can also be namespaced
                ["namespace-stubs/pkg/__init__.pyi"],
                {"pkg", "namespace-stubs", "namespace-stubs.pkg"},
            ),
            (
                # Just the top-level package can have `-stubs`, ignore nested ones
                ["namespace-stubs/pkg-stubs/__init__.pyi"],
                {"pkg", "namespace-stubs"}
            ),
            (["_hidden/file.py"], {"pkg"}),
            (["news/finalize.py"], {"pkg"}),
        ]
    )
    def test_flat_layout_with_extra_files(self, tmp_path, extra_files, pkgs):
        files = self.FILES["flat"] + extra_files
        _populate_project_dir(tmp_path, files, {})
        dist = _get_dist(tmp_path, {})
        assert set(dist.packages) == pkgs

    @pytest.mark.parametrize(
        "extra_files",
        [
            ["other/__init__.py"],
            ["other/finalize.py"],
        ]
    )
    def test_flat_layout_with_dangerous_extra_files(self, tmp_path, extra_files):
        files = self.FILES["flat"] + extra_files
        _populate_project_dir(tmp_path, files, {})
        with pytest.raises(PackageDiscoveryError, match="multiple (packages|modules)"):
            _get_dist(tmp_path, {})

    def test_flat_layout_with_single_module(self, tmp_path):
        files = self.FILES["single_module"] + ["invalid-module-name.py"]
        _populate_project_dir(tmp_path, files, {})
        dist = _get_dist(tmp_path, {})
        assert set(dist.py_modules) == {"pkg"}

    def test_flat_layout_with_multiple_modules(self, tmp_path):
        files = self.FILES["single_module"] + ["valid_module_name.py"]
        _populate_project_dir(tmp_path, files, {})
        with pytest.raises(PackageDiscoveryError, match="multiple (packages|modules)"):
            _get_dist(tmp_path, {})


class TestNoConfig:
    DEFAULT_VERSION = "0.0.0"  # Default version given by setuptools

    EXAMPLES = {
        "pkg1": ["src/pkg1.py"],
        "pkg2": ["src/pkg2/__init__.py"],
        "pkg3": ["src/pkg3/__init__.py", "src/pkg3-stubs/__init__.py"],
        "pkg4": ["pkg4/__init__.py", "pkg4-stubs/__init__.py"],
        "ns.nested.pkg1": ["src/ns/nested/pkg1/__init__.py"],
        "ns.nested.pkg2": ["ns/nested/pkg2/__init__.py"],
    }

    @pytest.mark.parametrize("example", EXAMPLES.keys())
    def test_discover_name(self, tmp_path, example):
        _populate_project_dir(tmp_path, self.EXAMPLES[example], {})
        dist = _get_dist(tmp_path, {})
        assert dist.get_name() == example

    def test_build_with_discovered_name(self, tmp_path):
        files = ["src/ns/nested/pkg/__init__.py"]
        _populate_project_dir(tmp_path, files, {})
        _run_build(tmp_path, "--sdist")
        # Expected distribution file
        dist_file = tmp_path / f"dist/ns.nested.pkg-{self.DEFAULT_VERSION}.tar.gz"
        assert dist_file.is_file()


@pytest.mark.parametrize(
    "folder, opts",
    [
        ("src", {}),
        ("lib", {"packages": "find:", "packages.find": {"where": "lib"}}),
    ]
)
def test_discovered_package_dir_with_attr_directive_in_config(tmp_path, folder, opts):
    _populate_project_dir(tmp_path, [f"{folder}/pkg/__init__.py", "setup.cfg"], opts)
    (tmp_path / folder / "pkg/__init__.py").write_text("version = 42")
    (tmp_path / "setup.cfg").write_text(
        "[metadata]\nversion = attr: pkg.version\n"
        + (tmp_path / "setup.cfg").read_text()
    )

    dist = _get_dist(tmp_path, {})
    assert dist.get_name() == "pkg"
    assert dist.get_version() == "42"
    assert dist.package_dir
    package_path = find_package_path("pkg", dist.package_dir, tmp_path)
    assert os.path.exists(package_path)
    assert folder in _Path(package_path).parts()

    _run_build(tmp_path, "--sdist")
    dist_file = tmp_path / "dist/pkg-42.tar.gz"
    assert dist_file.is_file()


def test_discovered_package_dir_with_attr_in_pyproject_config(tmp_path):
    _populate_project_dir(tmp_path, ["src/pkg/__init__.py"], {})
    (tmp_path / "src/pkg/__init__.py").write_text("version = 42")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'pkg'\ndynamic = ['version']\n"
        "[tool.setuptools.dynamic]\nversion = {attr = 'pkg.version'}\n"
    )
    dist = _get_dist(tmp_path, {})
    assert dist.get_version() == "42"
    assert dist.package_dir == {"": "src"}


def test_skip_when_extensions_are_provided(tmp_path):
    """Ensure that auto-discovery is not triggered when the project is based on
    C-Extensions only.
    """
    # This example is based on: https://github.com/nucleic/kiwi/tree/1.4.0
    files = [
        "benchmarks/file.py",
        "docs/Makefile",
        "docs/requirements.txt",
        "docs/source/conf.py",
        "proj/header.h",
        "proj/file.py",
        "py/proj.cpp",
        "py/other.cpp",
        "py/file.py",
        "py/py.typed",
        "py/tests/test_proj.py",
        "README.rst",
    ]
    _populate_project_dir(tmp_path, files, {})

    pyproject = """
        [project]
        name = 'proj'
        version = '42'
    """
    (tmp_path / "pyproject.toml").write_text(cleandoc(pyproject))

    setup_script = """
        from setuptools import Extension, setup

        ext_modules = [
            Extension(
                "proj",
                ["py/proj.cpp", "py/other.cpp"],
                include_dirs=["."],
                language="c++",
            ),
        ]
        setup(ext_modules=ext_modules)
    """
    (tmp_path / "setup.py").write_text(cleandoc(setup_script))
    dist = _get_dist(tmp_path, {})
    assert dist.get_name() == "proj"
    assert dist.get_version() == "42"
    assert dist.py_modules is None
    assert dist.packages is None
    assert len(dist.ext_modules) == 1
    assert dist.ext_modules[0].name == "proj"


def _populate_project_dir(root, files, options):
    # NOTE: Currently pypa/build will refuse to build the project if no
    # `pyproject.toml` or `setup.py` is found. So it is impossible to do
    # completely "config-less" projects.
    (root / "setup.py").write_text("import setuptools\nsetuptools.setup()")
    (root / "README.md").write_text("# Example Package")
    (root / "LICENSE").write_text("Copyright (c) 2018")
    _write_setupcfg(root, options)
    paths = (root / f for f in files)
    for path in paths:
        path.parent.mkdir(exist_ok=True, parents=True)
        path.touch()


def _write_setupcfg(root, options):
    if not options:
        print("~~~~~ **NO** setup.cfg ~~~~~")
        return
    setupcfg = ConfigParser()
    setupcfg.add_section("options")
    for key, value in options.items():
        if key == "packages.find":
            setupcfg.add_section(f"options.{key}")
            setupcfg[f"options.{key}"].update(value)
        elif isinstance(value, list):
            setupcfg["options"][key] = ", ".join(value)
        elif isinstance(value, dict):
            str_value = "\n".join(f"\t{k} = {v}" for k, v in value.items())
            setupcfg["options"][key] = "\n" + str_value
        else:
            setupcfg["options"][key] = str(value)
    with open(root / "setup.cfg", "w") as f:
        setupcfg.write(f)
    print("~~~~~ setup.cfg ~~~~~")
    print((root / "setup.cfg").read_text())


def _run_build(path, *flags):
    cmd = [sys.executable, "-m", "build", "--no-isolation", *flags, str(path)]
    return run(cmd, env={'DISTUTILS_DEBUG': '1'})


def _get_dist(dist_path, attrs):
    root = "/".join(os.path.split(dist_path))  # POSIX-style

    script = dist_path / 'setup.py'
    if script.exists():
        with _Path(dist_path):
            dist = distutils.core.run_setup("setup.py", {}, stop_after="init")
    else:
        dist = Distribution(attrs)

    dist.src_root = root
    dist.script_name = "setup.py"
    with _Path(dist_path):
        dist.parse_config_files()

    dist.set_defaults()
    return dist


def _run_sdist_programatically(dist_path, attrs):
    dist = _get_dist(dist_path, attrs)
    cmd = sdist(dist)
    cmd.ensure_finalized()
    assert cmd.distribution.packages or cmd.distribution.py_modules

    with quiet(), _Path(dist_path):
        cmd.run()

    return dist, cmd
