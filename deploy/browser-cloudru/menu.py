"""Private VM screen. The operator chooses menu items; no shell or network listener."""
import getpass
import os
from pathlib import Path
import select
import sys
import warnings

import controller
import kit


class Console:
    def __init__(self, state, screen):
        self.state = state
        self.screen = screen

    def say(self, text):
        print(text, file=self.screen, flush=True)

    def clear(self):
        self.screen.write("\033[2J\033[3J\033[H")
        self.screen.flush()

    def prompt(self, text):
        self.screen.write(text + " ")
        self.screen.flush()
        return input().strip()

    def password(self):
        return getpass.getpass("Setup password (hidden): ", stream=self.screen)

    def prepare(self, edit=False):
        password = self.password()
        fields = controller.unseal(Path("/etc/pc-browser-input.enc").read_bytes(), password)
        if edit:
            self.say("Enter a new value, or press Enter to keep the current value.")
            for name in ("project", "bucket", "key_id", "secret", "s3_key", "s3_secret"):
                value = getpass.getpass(name + " (hidden): ", stream=self.screen)
                if value:
                    fields[name] = value
        self.say("Checking the private S3 bucket and its versioning...")
        controller.initialize(self.state, fields, password)
        self.review()

    def review(self):
        controller.review(self.state, self.say)
        self.say("Plan ready. No HTTPS resources have been ordered yet.")

    def apply(self):
        plan = controller.setup.read(self.state / "https-plan.json")
        config = plan["config"]
        self.say("Project: " + config["project_id"] + "\nVM: " + config["origin_ip"] +
                 "\nSecurity group: " + config["security_group_id"] + "\nContainer: " + config["container_name"])
        self.say("Existing VM, disk, IPv4 and S3 are billed. This adds a private registry, a container "
                 "(0-1 replicas), secrets, storage and traffic, and permits TCP 9443 with mTLS.")
        self.say("Check prices and quotas in Cloud.ru before approving.")
        controller.unlock(self.state, self.password())
        if self.prompt("Type YES to accept charges and apply this plan:") != "YES":
            self.say("Plan saved. You can return to this VM later.")
            return
        self.say("Installing memory, HTTPS and backups. This may take several minutes...")
        self.say(controller.install(self.state, plan["plan_sha256"]))

    def enroll(self):
        url, login, code = controller.login(self.state, self.password())
        self.clear()
        self.say("Open on YOUR device: " + login + "\nOne-time code (valid 20 min): " + code +
                 "\nMCP URL: " + url + "\nPress Enter to hide (also clears in 60 seconds).")
        if select.select([sys.stdin], [], [], 60)[0]:
            input()
        self.clear()

    def export(self):
        name = kit.export(self.state, self.password())
        self.say("Verified encrypted recovery kit in your S3 bucket: " + name)

    def run(self):
        while True:
            self.clear()
            self.say("Portable Context | Cloud.ru setup")
            if (self.state / "https-installed.json").is_file():
                self.say("MCP URL: " + controller.setup.read(self.state / "https-installed.json")["mcp_url"])
                choices = [("Create passkey / owner sign-in", self.enroll), ("Export recovery kit to S3", self.export)]
            elif (self.state / "https-plan.json").is_file():
                choices = [("Review costs and install / resume", self.apply)]
            elif (self.state / "browser-prepared.json").is_file():
                choices = [("Check project and prepare plan", self.review), ("Correct credentials", lambda: self.prepare(True))]
            else:
                choices = [("Unlock settings and prepare plan", self.prepare), ("Correct credentials", lambda: self.prepare(True))]
            for number, (label, _) in enumerate(choices, 1):
                self.say(f"{number}. {label}")
            value = self.prompt("Choose a number, then Enter:")
            if value not in {str(i) for i in range(1, len(choices) + 1)}:
                continue
            try:
                choices[int(value) - 1][1]()
            except controller.InputError as error:
                self.say(str(error))
            except Exception as error:
                self.say(f"Step stopped ({type(error).__name__}). Check project, permissions and network. "
                         "Return to this VM to retry. Resources are retained.")
            self.prompt("Press Enter to return to the menu.")


if __name__ == "__main__":
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Private VM terminal required")
    os.umask(0o077)
    warnings.simplefilter("error", getpass.GetPassWarning)
    # Only messages explicitly addressed to the owner reach the physical screen.
    # Terraform and other inherited child-process outputs can contain private data.
    with os.fdopen(os.dup(1), "w", buffering=1) as screen, open(os.devnull, "w") as null:
        os.dup2(null.fileno(), 1)
        os.dup2(null.fileno(), 2)
        state = controller.setup.private_directory(Path.home() / ".local/share/pc-browser-state")
        Console(state, screen).run()
