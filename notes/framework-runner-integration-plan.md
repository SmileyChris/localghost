# Framework runner integration plan

This document records how the root-aware framework runner branch should be
integrated with `feat/configured-run-manager`. It is a development plan rather
than user-facing product documentation.

## Objective

Keep one host-run planning model for automatically detected, explicitly
configured, foreground, and detached applications. Invoking Localghost from a
nested public directory must produce the same name, route, and managed session
as invoking it from the application root.

## Integration order

1. Rebase `feat/configured-run-manager` onto the current `main` branch.
2. Merge the root-aware framework runner branch.
3. Resolve the CLI run-flow overlap using the precedence rules below.
4. Add configured and detached integration tests before merging either feature
   into `main`.

## Directory model

`RunPlan.project_root` is the stable identity of an application. It supplies
the default name, `.env`, bridge source-path label, session matching path, and
session metadata path.

`RunPlan.working_directory` is where the application command executes. It is
normally the project root, but legacy CakePHP uses `app/webroot`.

The invocation directory is only the starting point for discovery and the base
for an explicitly supplied relative `--config` path.

## Configuration precedence

1. Explicit command-line options and command arguments.
2. Values from the selected `.localghost.toml` file.
3. Framework detection and its defaults.

Compose mode does not perform framework discovery. For host mode, discover the
framework root before looking for the default `.localghost.toml`, so a command
started inside `public` or `webroot` finds configuration stored at the project
root. An explicit `--config` path remains authoritative.

A configured custom command uses the directory containing `.localghost.toml`
as its project root and working directory unless the configuration format later
adds an explicit working-directory field.

## Session behavior

- Foreground and detached launch paths use `plan.working_directory`.
- Session matching and creation use `plan.project_root`, not the invocation
  directory.
- Starting the same application from its root and a nested directory finds the
  same detached session.
- Session output records both directories when they differ.

## Required integration coverage

- Default configuration is found when invoked from CakePHP `webroot` or Laravel
  `public`.
- Explicit `--config` remains relative to the invocation directory.
- CLI values override config values, and config values override detection.
- Foreground and detached legacy CakePHP processes start in `app/webroot`.
- Detached session matching is stable across invocation directories.
- Compose mode bypasses PHP and JavaScript framework ambiguity.
- Existing Django settings checks run from the detected project root.
