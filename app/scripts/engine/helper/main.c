/* The engine's helper app: brindle's engine under a name macOS can show.
 *
 * macOS takes everything a process does to its "responsible process". The
 * daemon is a launchd job, so that is the daemon itself, and tmux and every
 * agent's pane descend from it. Run as the engine's bare python3, privacy
 * prompts and System Settings named "python3" and gave no reason. This is the
 * main executable of an app bundle (Info.plist beside this file: a name, usage
 * strings), so they name brindle (docs/investigations/macos-prompts.md).
 *
 * It runs Python in this process, from the engine's libpython: exec'ing
 * python3 would make the process python3 again. It runs only what the engine's
 * launcher (scripts/engine/brindle) runs, `-I -B [-X brindle-build=<id>] -m
 * brindle …`, and only the engine beside it: what holds brindle's grants runs
 * brindle, not any script it is handed. -I also keeps PYTHON* variables out.
 *
 * sys.executable stays the engine's python3, so everything brindle starts by
 * it (hooks, MCP servers, its own commands) is exactly what the launcher's
 * brindle starts: one hook command for Codex to trust, whichever ran first.
 *
 * Built by build_engine.sh against the engine's own Python headers (PyConfig's
 * layout is per version), for the bundle's layout:
 *
 *   Contents/Library/LoginItems/brindle.app/Contents/MacOS/brindle   this
 *   Contents/Resources/engine/python                                 the engine's Python
 */
#include <Python.h>

#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The engine, from this executable's directory. */
#define ENGINE "/../../../../../Resources/engine"

/* Whether argv is what the engine's launcher runs. */
static int launchers(int argc, char **argv)
{
    int i = 3;
    if (argc < 5 || strcmp(argv[1], "-I") != 0 || strcmp(argv[2], "-B") != 0) {
        return 0;
    }
    if (strcmp(argv[i], "-X") == 0) {
        if (strncmp(argv[i + 1], "brindle-build=", 14) != 0) {
            return 0;
        }
        i += 2;
    }
    return i + 1 < argc && strcmp(argv[i], "-m") == 0 && strcmp(argv[i + 1], "brindle") == 0;
}

static PyStatus configure(PyConfig *config, const char *home, const char *python, int argc, char **argv)
{
    PyStatus status = PyConfig_SetBytesString(config, &config->home, home);
    if (PyStatus_Exception(status)) {
        return status;
    }
    status = PyConfig_SetBytesString(config, &config->executable, python);
    if (PyStatus_Exception(status)) {
        return status;
    }
    status = PyConfig_SetBytesArgv(config, argc, argv);
    if (PyStatus_Exception(status)) {
        return status;
    }
    return Py_InitializeFromConfig(config);
}

int main(int argc, char **argv)
{
    char exe[PATH_MAX], real[PATH_MAX], path[PATH_MAX], home[PATH_MAX], python[PATH_MAX];
    uint32_t size = sizeof exe;

    if (!launchers(argc, argv)) {
        fputs("brindle's engine helper runs only brindle (-I -B [-X brindle-build=<id>] -m brindle ...), "
              "for brindle.app's daemon. In a terminal, use `brindle`.\n", stderr);
        return 2;
    }
    if (_NSGetExecutablePath(exe, &size) != 0 || realpath(exe, real) == NULL) {
        fputs("brindle's engine helper can't find itself\n", stderr);
        return 1;
    }
    if (snprintf(path, sizeof path, "%s" ENGINE "/python", dirname(real)) >= (int)sizeof path
        || realpath(path, home) == NULL
        || snprintf(python, sizeof python, "%s/bin/python3", home) >= (int)sizeof python) {
        fprintf(stderr, "brindle's engine helper found no engine at %s\n", path);
        return 1;
    }

    /* As python3's own main does, with the engine's home and executable given. */
    PyPreConfig preconfig;
    PyPreConfig_InitPythonConfig(&preconfig);
    PyStatus status = Py_PreInitializeFromBytesArgs(&preconfig, argc, argv);
    if (PyStatus_Exception(status)) {
        Py_ExitStatusException(status);
    }
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    status = configure(&config, home, python, argc, argv);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        Py_ExitStatusException(status);
    }
    return Py_RunMain();
}
