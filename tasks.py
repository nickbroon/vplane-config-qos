#!/usr/bin/env python3
# Copyright (c) 2021, AT&T Intellectual Property. All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only

# *************************************************
# Developer---->|                  |----> External tool (eg. gitlint)
#               |---->tasks.py---->|
# Jenkins ----->|                  |----> External tool (eg. mypy)
#
# Jenkinsfile should be a simple & thin wrapper that calls functionality in tasks.py
# All functions with a @task decorator are individual stages that perform some check.
#
# Tasks are accesible from the command line by typing `invoke {TASKNAME}`
# Type `invoke -l` to see a list of available commands.
#
# Either all files can be checked or only changed files.
# By default tasks.py will compare the branch you are on with master to see what has changed.
# If you specify `--commits all` (eg `invoke flake8 --commits all`) then all files will be checked.
# **************************************************

import sys
import re
import subprocess
import datetime
from typing import List
import magic
from invoke import task
import os
import functools

# ***************************************************
# Helper functions used by multiple stages
# ***************************************************


@functools.lru_cache(maxsize=1)
def get_files(commits: str) -> List:
    def get_all_files(repo_root: str) -> List:
        """Return every file in this repository. Ignore .git folder and files excluded by .gitignore"""
        # Most tools can search for files themselves and do not need to be passed a list of files.
        # For example `flake8 .` will find every .py file recursively. This repo contains scripts without
        # any file extension. Therefore it is necessary to pass every file to these tools so no files are
        # left out.

        all_files = []
        for root, dirs, files in os.walk(repo_root):
            if ".git" in dirs:                          # Ignore any files in .git/
                dirs.remove(".git")
            for file in files:
                all_files.append(os.path.join(root, file))

        # Ignore any files in .gitignore
        all_files_str = " ".join(all_files)
        ignored_files = subprocess.check_output(
            [f"git check-ignore {all_files_str}"], shell=True).decode("utf-8").rstrip().split()
        valid_files = list(set(all_files) - set(ignored_files))
        return valid_files

    def get_changed_files(repo_root: str, commits: str) -> List:
        """Return all the files that have changed"""

        git_command = f"git diff --diff-filter=rd --find-renames=100% --name-only --format=format:'' {commits}"
        result = subprocess.check_output(git_command, shell=True).decode("utf-8")
        changed_files = result.splitlines()

        print(f"Files to check {changed_files}\n", flush=True)
        changed_files_full_path = [repo_root + '/' + s for s in changed_files]
        return changed_files_full_path

    # Get the root of the git repo. e.g /home/ag474u/Code/vplane-config-qos.
    repo_root = subprocess.check_output(['git', 'rev-parse', '--show-toplevel']).decode("utf-8").rstrip()

    if commits == "all":
        return get_all_files(repo_root)
    else:
        return get_changed_files(repo_root, commits)


def get_files_by_types(files: List, types: List[str]) -> List:
    """Return a subset of 'files' based of the file type. Use python-magic rather simply
        looking at the file extension because some of the scripts do not have any file extension"""

    files_by_types = []
    for file in files:
        file_type = magic.from_file(file)
        if any(specified_type in file_type for specified_type in types):
            files_by_types.append(file)
        # python-magic does not recognise yang files so find them using their file extension
        else:
            if "Yang" in types:
                file_extension = file.split('.').pop()
                if file_extension == "yang":
                    files_by_types.append(file)

    return files_by_types

# ***************************************************
# Stages of the pipeline
# ***************************************************


@task
def flake8(context, commits="master...HEAD"):
    """Run flake8 over changed files"""
    files = get_files(commits)
    python_files = get_files_by_types(files, ["Python"])
    if python_files:  # Only run flake8 if there are files to check (otherwise it will run it over the directory)
        python_files = " ".join(python_files)
        context.run(f"python3 -m flake8 --count {python_files}", echo=True)


@task
def mypy(context, commits="master...HEAD"):
    """Run python static type checker"""
    files = get_files(commits)
    python_files = get_files_by_types(files, ["Python"])
    if python_files:  # Only run mypy if there are files to check (otherwise it will run it over the directory)
        python_files = " ".join(python_files)
        context.run(f"mypy {python_files}", echo=True)


@task
def pytest(context):
    """Run the unit test suite"""
    context.run("python3-coverage run --source . -m pytest", echo=True)


@task(pre=[pytest])
def coverage(context):
    """Generate the coverage report for the unit test suite"""
    context.run("python3-coverage html", echo=True)
    context.run("python3-coverage report", echo=True)


@task
def gitlint(context, commits="master...HEAD"):
    """Run the unit test suite"""
    # context.run() fails for gitlint. Possibly because of https://github.com/fabric/fabric/issues/1812
    # So invoke gitlint using subprocess.run rather than invokes context.run
    command = f"gitlint --commits {commits}"
    print(f"Running: {command}", flush=True)
    output = subprocess.run(command, shell=True)
    if output.returncode:
        sys.exit(output.returncode)


@task
def licence(context, commits="master...HEAD"):
    """Check source code files contain the spdx licence and an up to date AT&T licence"""

    def check_att_licence(source_files: List[str]) -> bool:
        for file in source_files:
            year = datetime.datetime.now().year
            pattern = rf"Copyright \(c\) .*{year}.* AT&T Intellectual Property"
            error = False

            with open(file) as f:
                for line in f:
                    match = re.search(pattern, line)
                    if match:
                        break
                else:
                    print(f"Failed: File {file} does not contain AT&T licence for the current year ({year})")
                    error = True
        return error

    def check_spdx_licence(source_files: List[str]) -> bool:
        error = False
        for file in source_files:
            pattern = r"SPDX-License-Identifier:"
            with open(file) as f:
                if pattern not in f.read():
                    print(f"Failed: File {file} does not contain SPDX licence")
                    error = True
        return error

    files = get_files(commits)
    code_files = get_files_by_types(files, ["Python", "Perl", "Bourne-Again shell", "Yang"])
    att_error = check_att_licence(code_files)
    spdx_error = check_spdx_licence(code_files)
    if att_error or spdx_error:
        sys.exit(1)


@task
def yang(context, commits="master...HEAD"):
    """Run dram and check yang address"""
    def check_yang_address(yang_files: List[str]):

        pattern = r"Postal: 208 S\. Akard Street\n\s*Dallas\, TX 75202, USA\n\s*Web: www.att.com"
        error = False
        for file in yang_files:
            with open(file) as f:
                yang = f.read()
                match = re.search(pattern, yang)
                if not match:
                    print(f"Failed: Yang file {file} does not contain correct address")
                    error = True
        if error:
            sys.exit(1)

    def run_dram(yang_files: List[str]):

        filenames = []
        platform_yang = []

        # TODO: seperate yang files into different folders, eg. platform folder, deviation folder, etc
        for file in yang_files:
            if "deviation" in file:
                platform_yang.append(file)
            else:
                filenames.append(file)

        filenames = ",".join(filenames)
        platform_yang = ",".join(platform_yang)

        dram_command = "dram --username jenkins --suppress"
        if filenames:
            dram_command += f" --filenames {filenames}"
        if platform_yang:
            dram_command += f"--platform-yang {platform_yang}"

        context.run(dram_command, echo=True)

    files = get_files(commits)
    yang_files = get_files_by_types(files, ["Yang"])
    # If there are no yang changes then can skip this
    if yang_files:
        check_yang_address(yang_files)
        run_dram(yang_files)


@task
def build(context):
    """Build the debian packages.
       Copy packages from parent directory to new child directory"""
    context.run(f"dpkg-buildpackage", echo=True)
    context.run(f"mkdir -p deb_packages", echo=True)
    context.run(f"cp ../*.deb ./deb_packages/", echo=True)


@task(pre=[flake8, mypy, pytest, coverage, gitlint, licence, yang, build])
def all(context, commits="master...HEAD"):
    """Run all stages in the pipeline."""
    # Use invoke pre tasks to call each stage
    # If no stage has called exited early then all stages were succesful
    print("\nSUCCESS")
