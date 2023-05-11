#!/usr/bin/env python

import os
import sys
import re
import traceback
import time
from openai.error import RateLimitError

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.live import Live
from rich.text import Text
from rich.markdown import Markdown
from pathlib import Path

import git
import openai

from aider.dump import dump
from aider.getinput import get_input
from aider import utils
from aider import prompts
from aider.commands import Commands

openai.api_key = os.getenv("OPENAI_API_KEY")


class Coder:
    abs_fnames = set()

    last_modified = 0
    repo = None
    last_aider_commit_hash = None

    def __init__(self, main_model, fnames, pretty, history_file, show_diffs):
        self.history_file = history_file

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=True, no_color=True)

        self.commands = Commands(self.console, self)
        self.main_model = main_model
        if main_model == "gpt-3.5-turbo":
            self.console.print(
                f"[red]This tool will almost certainly fail to work with {main_model}"
            )

        self.set_repo(fnames)

        if not self.repo:
            self.console.print(
                "[red]No suitable git repo, will not automatically commit edits."
            )
            self.find_common_root()

        self.pretty = pretty
        self.show_diffs = show_diffs

    def find_common_root(self):
        common_prefix = os.path.commonpath(list(self.abs_fnames))
        self.root = os.path.dirname(common_prefix)
        self.console.print(f"[bright_black]Common root directory: {self.root}")

    def set_repo(self, cmd_line_fnames):
        if not cmd_line_fnames:
            cmd_line_fnames = ["."]

        abs_fnames = [Path(fn).resolve() for fn in cmd_line_fnames]

        repo_paths = []
        for fname in abs_fnames:
            if not fname.exists():
                self.console.print(f"[bright_black]Creating {fname}")
                fname.touch()
            try:
                repo_path = git.Repo(fname, search_parent_directories=True).git_dir
                repo_paths.append(repo_path)
            except git.exc.InvalidGitRepositoryError:
                pass
        num_repos = len(set(repo_paths))

        if num_repos == 0:
            self.console.print("[red]Files are not in a git repo.")
            return
        if num_repos > 1:
            self.console.print("[red]Files are in different git repos.")
            return

        # https://github.com/gitpython-developers/GitPython/issues/427
        repo = git.Repo(repo_paths.pop(), odbt=git.GitDB)

        self.root = repo.working_tree_dir

        new_files = []
        for fname in abs_fnames:
            if fname.is_dir():
                continue
            self.console.print(f"[bright_black]Loading {fname}")

            fname = fname.resolve()
            self.abs_fnames.add(str(fname))

            relative_fname = os.path.relpath(fname, repo.working_tree_dir)
            tracked_files = set(repo.git.ls_files().splitlines())
            if relative_fname not in tracked_files:
                new_files.append(relative_fname)

        if new_files:
            self.console.print(f"[bright_black]Files not tracked in {repo.git_dir}:")
            for fn in new_files:
                self.console.print(f"[bright_black]  {fn}")
            if Confirm.ask(
                "[bright_black]Add them?", console=self.console, default="y"
            ):
                for relative_fname in new_files:
                    repo.git.add(relative_fname)
                    self.console.print(
                        f"[bright_black]Added {relative_fname} to the git repo"
                    )
                show_files = ", ".join(new_files)
                commit_message = (
                    f"Initial commit: Added new files to the git repo: {show_files}"
                )
                repo.git.commit("-m", commit_message, "--no-verify")
                self.console.print(
                    f"[bright_black]Committed new files with message: {commit_message}"
                )
            else:
                self.console.print("[red]Skipped adding new files to the git repo.")
                return

        self.repo = repo

    def get_files_content(self, fnames=None):
        if not fnames:
            fnames = self.abs_fnames

        prompt = ""
        for fname in fnames:
            relative_fname = os.path.relpath(fname, self.root)
            prompt += utils.quoted_file(fname, relative_fname)
        return prompt

    def get_last_modified(self):
        if self.abs_fnames:
            return max(Path(fname).stat().st_mtime for fname in self.abs_fnames)
        return 0

    def get_files_messages(self):
        files_content = prompts.files_content_prefix
        files_content += self.get_files_content()

        all_content = files_content

        if self.repo is not None:
            tracked_files = set(self.repo.git.ls_files().splitlines())
            files_listing = "\n".join(tracked_files)
            repo_content = prompts.repo_content_prefix
            repo_content += files_listing

            all_content = repo_content + "\n\n" + files_content

        files_messages = [
            dict(role="user", content=all_content),
            dict(role="assistant", content="Ok."),
            dict(
                role="system",
                content=prompts.files_content_suffix + prompts.system_reminder,
            ),
        ]

        return files_messages

    def run(self):
        self.done_messages = []
        self.cur_messages = []

        self.num_control_c = 0

        while True:
            try:
                self.run_loop()
            except KeyboardInterrupt:
                self.num_control_c += 1
                if self.num_control_c >= 2:
                    break
                self.console.print("[red]^C again to quit")
            except EOFError:
                return

    def run_loop(self):
        if self.pretty:
            self.console.rule()
        else:
            print()

        inp = get_input(self.history_file, self.abs_fnames, self.commands)

        if inp.startswith("/"):
            inp = self.commands.run(inp)
            if not inp:
                return

        self.num_control_c = 0

        if self.last_modified < self.get_last_modified():
            self.commit(ask=True)

            # files changed, move cur messages back behind the files messages
            self.done_messages += self.cur_messages
            self.done_messages += [
                dict(role="user", content=prompts.files_content_local_edits),
                dict(role="assistant", content="Ok."),
            ]
            self.cur_messages = []

        if not inp:
            return

        self.cur_messages += [
            dict(role="user", content=inp),
        ]

        messages = [
            dict(role="system", content=prompts.main_system + prompts.system_reminder),
        ]
        messages += self.done_messages
        messages += self.get_files_messages()
        messages += self.cur_messages

        # utils.show_messages(messages, "all")

        content, interrupted = self.send(messages)
        if interrupted:
            content += "\n^C KeyboardInterrupt"

        Path(".aider.last.md").write_text(content)

        self.cur_messages += [
            dict(role="assistant", content=content),
        ]

        self.console.print()
        if interrupted:
            return True

        try:
            edited = self.update_files(content, inp)
        except Exception as err:
            print(err)
            print()
            traceback.print_exc()
            edited = None

        if not edited:
            return True

        res = self.commit(history=self.cur_messages, prefix="aider: ")
        if res:
            commit_hash, commit_message = res
            self.last_aider_commit_hash = commit_hash

            saved_message = prompts.files_content_gpt_edits.format(
                hash=commit_hash,
                message=commit_message,
            )
        else:
            self.console.print("[red]Warning: no changes found in tracked files.")
            saved_message = prompts.files_content_gpt_no_edits

        self.done_messages += self.cur_messages
        self.done_messages += [
            dict(role="user", content=saved_message),
            dict(role="assistant", content="Ok."),
        ]
        self.cur_messages = []
        return True

    def send(self, messages, model=None, silent=False):
        if not model:
            model = self.main_model

        self.resp = ""
        interrupted = False
        try:
            while True:
                try:
                    completion = openai.ChatCompletion.create(
                        model=model,
                        messages=messages,
                        temperature=0,
                        stream=True,
                    )
                    break
                except RateLimitError:
                    retry_after = 1
                    # print(f"Rate limit exceeded. Retrying in {retry_after} seconds.")
                    time.sleep(retry_after)

            if self.pretty and not silent:
                self.show_send_output_color(completion)
            else:
                self.show_send_output_plain(completion, silent)
        except KeyboardInterrupt:
            interrupted = True

        return self.resp, interrupted

    def show_send_output_plain(self, completion, silent):
        for chunk in completion:
            if chunk.choices[0].finish_reason not in (None, "stop"):
                dump(chunk.choices[0].finish_reason)
            try:
                text = chunk.choices[0].delta.content
                self.resp += text
            except AttributeError:
                continue

            if not silent:
                sys.stdout.write(text)
                sys.stdout.flush()

    def show_send_output_color(self, completion):
        with Live(vertical_overflow="scroll") as live:
            for chunk in completion:
                if chunk.choices[0].finish_reason not in (None, "stop"):
                    assert False, "Exceeded context window!"
                try:
                    text = chunk.choices[0].delta.content
                    self.resp += text
                except AttributeError:
                    continue

                md = Markdown(self.resp, style="blue", code_theme="default")
                live.update(md)

            # live.update(Text(""))
            # live.stop()

        # md = Markdown(self.resp, style="blue", code_theme="default")
        # self.console.print(md)

    pattern = re.compile(
        r"(^```\S*\s*)?^((?:[a-zA-Z]:\\|/)?(?:[\w\s.-]+[\\/])*\w+(\.[\w\s.-]+)*)\s+(^```\S*\s*)?^<<<<<<< ORIGINAL\n(.*?\n?)^=======\n(.*?)^>>>>>>> UPDATED",  # noqa: E501
        re.MULTILINE | re.DOTALL,
    )

    def update_files(self, content, inp):
        edited = set()
        for match in self.pattern.finditer(content):
            _, path, _, _, original, updated = match.groups()

            path = path.strip()

            full_path = os.path.abspath(os.path.join(self.root, path))

            if full_path not in self.abs_fnames:
                if not Path(full_path).exists():
                    question = f"[bright_black]Allow creation of new file {path}?"
                else:
                    question = f"[bright_black]Allow edits to {path} which was not previously provided?"
                if not Confirm.ask(question, console=self.console, default="y"):
                    self.console.print(f"[red]Skipping edit to {path}")
                    continue

                Path(full_path).touch()
                self.abs_fnames.add(full_path)

                if self.repo and Confirm.ask(
                    f"[bright_black]Add {path} to git?",
                    console=self.console,
                    default="y",
                ):
                    self.repo.git.add(full_path)

            edited.add(path)
            if utils.do_replace(full_path, original, updated):
                self.console.print(f"[bright_black]Applied edit to {path}")
            else:
                self.console.print(f"[red]Failed to apply edit to {path}")

        return edited

    def commit(self, history=None, prefix=None, ask=False, message=None):
        repo = self.repo
        if not repo:
            return

        if not repo.is_dirty():
            return

        diffs = ""
        dirty_fnames = []
        relative_dirty_fnames = []
        for fname in self.abs_fnames:
            relative_fname = os.path.relpath(fname, repo.working_tree_dir)
            if self.pretty:
                these_diffs = repo.git.diff("HEAD", "--color", relative_fname)
            else:
                these_diffs = repo.git.diff("HEAD", relative_fname)

            if these_diffs:
                dirty_fnames.append(fname)
                relative_dirty_fnames.append(relative_fname)
                diffs += these_diffs + "\n"

        if not dirty_fnames:
            self.last_modified = self.get_last_modified()
            return

        if self.show_diffs or ask:
            self.console.print(Text(diffs))

        diffs = "# Diffs:\n" + diffs

        # for fname in dirty_fnames:
        #    self.console.print(f"[red]  {fname}")

        context = ""
        if history:
            context += "# Context:\n"
            for msg in history:
                context += msg["role"].upper() + ": " + msg["content"] + "\n"

        messages = [
            dict(role="system", content=prompts.commit_system),
            dict(role="user", content=context + diffs),
        ]

        # if history:
        #    self.show_messages(messages, "commit")

        commit_message, interrupted = self.send(
            messages,
            model="gpt-3.5-turbo",
            silent=True,
        )

        commit_message = commit_message.strip().strip('"').strip()

        if interrupted:
            self.console.print(
                "[red]Unable to get commit message from gpt-3.5-turbo. Use /commit to try again.\n"
            )
            return

        if prefix:
            commit_message = prefix + commit_message

        if ask:
            self.last_modified = self.get_last_modified()

            self.console.print("[bright_black]Files have uncommitted changes.\n")
            self.console.print(
                f"[bright_black]Suggested commit message:\n{commit_message}\n"
            )

            res = Prompt.ask(
                "[bright_black]Commit before the chat proceeds? \[y/n/commit message]",  # noqa: W605
                console=self.console,
                default="y",
            ).strip()
            self.console.print()

            if res.lower() in ["n", "no"]:
                self.console.print("[red]Skipped commmit.")
                return
            if res.lower() not in ["y", "yes"] and res:
                commit_message = res

        repo.git.add(*relative_dirty_fnames)

        full_commit_message = commit_message + "\n\n" + context
        repo.git.commit("-m", full_commit_message, "--no-verify")
        commit_hash = repo.head.commit.hexsha[:7]
        self.console.print(f"[bright_black]{commit_hash} {commit_message}")

        self.last_modified = self.get_last_modified()

        return commit_hash, commit_message

    def get_inchat_relative_files(self):
        files = [os.path.relpath(fname, self.root) for fname in self.abs_fnames]
        return sorted(set(files))

    def get_all_relative_files(self):
        if self.repo:
            files = self.repo.git.ls_files().splitlines()
        else:
            files = self.get_inchat_relative_files()

        return sorted(set(files))
