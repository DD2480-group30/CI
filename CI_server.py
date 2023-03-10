import os
import shutil
import sys
import re
import git
import yaml
import json
import glob
import requests
import subprocess
import socketserver
from typing import Tuple
from time import strftime, gmtime
from http.server import BaseHTTPRequestHandler, HTTPServer


class CIServer(BaseHTTPRequestHandler):
    def __init__(self, request: bytes, client_address: Tuple[str, int],
                 server: socketserver.BaseServer):
        super().__init__(request, client_address, server)
        self.payload = []
        self.token = ''

    def response(self, message="Default", content_type="text/plain"):
        """
        HTTP response to webhook
        """
        self.send_response(200)
        self.send_header('Content-type', content_type)
        self.end_headers()
        self.wfile.write(bytes(message, "utf-8"))

    def do_GET(self):
        """
        Handles incoming GET requests.
        Lists the build results in HTML format with links to each result.
        """
        print(self.headers)
        if self.path == '/':
            self.response()
        elif self.path == '/results':
            with open('results.html', 'r') as skeleton:
                result_list = ''
                for file in glob.glob("results/*/*/*", recursive=True):
                    result_list += (f'<a href="{file}">' +
                                    file.replace("results/", "") +
                                    '</a> <br />\n')
                message = skeleton.read().format(results=result_list)
            self.response(message=message, content_type="text/html")
        elif re.fullmatch(r'/results/.*', self.path):
            with open(self.path.replace("/", "", 1)) as f:
                self.response(message=f.read())
        elif re.match(r'^/die', self.path):
            token = read_token()
            g = re.search(r'/die\?auth=(.*)', self.path).groups()[0]
            if g == token:
                self.response(message="Shutting down server")
                raise KeyboardInterrupt

    def do_POST(self):
        """
        Handles incoming POST requests.
        Receives the payload from GitHub and extracts relevant information.
        Calls the main CI functions using this information.
        """
        CI = CIServerHelper()
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        event = CI.parse_header(self.headers)
        self.payload = json.loads(post_data.decode('utf-8'))
        commit_id = self.payload["after"]
        clone_url = self.payload["repository"]["clone_url"]
        repo_name = self.payload["repository"]["name"]
        branch = self.payload["ref"].replace("refs/heads/", "")
        self.response(f'Received Event: {event}, ' +
                      f'Commit_id: {commit_id}, Clone_url: {clone_url}')
        repo = CI.clone_repo(clone_url, branch, repo_name)

        build_result, test_result = (False, ''), (False, '')
        if event == "push":
            build_result = CI.ci_build(repo)
            test_result = CI.ci_test(repo)
        repo_full_name = self.payload["repository"]["full_name"]
        statuses_url = self.payload["repository"]["statuses_url"]
        target_url = CI.log_results(repo_full_name, commit_id,
                                    build_result, test_result)
        CI.send_results(commit_id, build_result, test_result,
                        statuses_url, target_url)


class CIServerHelper:
    def parse_header(self, header):
        """
        Parsing of http header
        """
        if 'X-Github-Event' in header:
            event = header['X-Github-Event']
        else:
            event = "Unknown event"
        return event

    def clone_repo(self, clone_url, branch, repo_name):
        """
        Clones a repo specified in webhook payload to the server, or fetch if the repo
        is already present locally
        """
        dir_name = os.path.dirname(os.path.realpath(__file__))
        new_dir = "CI-clonedir/" + repo_name
        repo_path = os.path.join(dir_name, new_dir)

        # Check for existing repo and delete if it exists
        if os.path.exists(repo_path):
            shutil.rmtree(repo_path)
        git.Repo.clone_from(clone_url, repo_path)
        repo = git.Repo(repo_path)

        # Check out branch specified in webhook payload.
        repo.git.checkout(branch)

        return repo

    def log_results(self, name, commit_id, build_result, test_result):
        """
        Log the results of build_result and test_result to persistent storage
        Returns the URL to view the results.
        """
        log_dir = os.path.join('results', name)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_file = os.path.join(log_dir, commit_id)

        f = open(log_file, 'w')

        token = read_token()
        br_scrubbed = re.sub(token, 'REDACTED', build_result[1])
        tr_scrubbed = re.sub(token, 'REDACTED', test_result[1])
        f.write("Log from " + strftime("%Y-%m-%d %H:%M:%S", gmtime()) + "\n\n")
        if build_result[0]:
            f.write("Lint and build successful!\n\n")
        else:
            f.write("Lint or build failed!\n\n")
        f.write(f"Message:\n{br_scrubbed}\n")
        f.write("\n----\n")

        if test_result[0]:
            f.write("Tests successful!\n\n")
        else:
            f.write("Tests failed!\n\n")
        f.write(f"Message:\n{tr_scrubbed}\n")
        f.close()

        # build URL
        with open('config.yml') as fin:
            data = yaml.load(fin, Loader=yaml.FullLoader)
        url = "http://" + data["HOSTNAME"] + ":" + str(data["PORT"]) + "/" + log_file
        return url

    def send_results(self, commit_id, build_result, test_result,
                     statuses_url, target_url):
        """
        Set the commit status on GitHub for commit_id
        according to build_result and test_result
        """
        # statuses_url is on the format
        # "https://api.github.com/repos/{owner}/{repo}/statuses/{sha}"
        # owner and repo is already set, therefore we set sha here
        statuses_url = statuses_url.format(sha=commit_id)
        # Token, fetch from local YML-file
        token = read_token()

        build_and_test = "failure"
        if build_result[0] and test_result[0]:
            build_and_test = "success"

        headers = {"Accept": "application/vnd.github+json",
                   "Authorization": "Bearer " + token,
                   "X-GitHub-Api-Version": "2022-11-28"}
        payload = {"state": build_and_test,
                   "description": "Build succeeded " + str(build_result[0]) +
                                  " Test succeeded " + str(test_result[0]),
                   "target_url": target_url}

        # TODO: complete feature after log_results
        requests.post(url=statuses_url, headers=headers, json=payload)

    def ci_build(self, repo, filepath="workflow.yml"):
        """
        Read from workflow file and execute related jobs if triggerred.
        Return a tuple of (boolean, string) that contains the build result
        """
        path = repo.working_dir + '/' + filepath
        with open(path) as fin:
            work = yaml.load(fin, Loader=yaml.FullLoader)

        main_dir = os.getcwd()
        os.chdir(repo.working_dir)

        # Find the jobs to be executed
        for job in work["jobs"]:
            # Skip the test part
            if job["name"] == 'Run tests':
                continue
            # Split the tasks to run
            tasks = job["run"].splitlines()
            # Print the current job name
            print("CI Server Build: " + job["name"])

            if job["name"] == 'Lint code':
                # Execute the shell commands
                for task in tasks:
                    # Execute the shell commands
                    result = subprocess.run(task, shell=True, text=True,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    # Return at once if build fails
                    # (ruff print the error message in stdout)
                    if result.stdout != "":
                        os.chdir(main_dir)
                        return False, result.stdout
            elif (job["name"] == 'Build project'
                  or job["name"] == 'Install dependencies'):
                for task in tasks:
                    # Execute the shell commands
                    result = subprocess.run(task, shell=True, text=True,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    # Print the output of the shell commands
                    print(result.stdout)
                    # Return at once if build fails
                    if result.stderr != "":
                        os.chdir(main_dir)
                        return False, result.stderr
            else:
                print("ERROR:", "Unrecognized job name!", file=sys.stderr)

        os.chdir(main_dir)
        return True, "Good News: All is Fine."

    def ci_test(self, repo, filepath="workflow.yml"):
        """
        Read from workflow file and execute related jobs if triggerred.
        Return a tuple of (boolean, string) that contains the test result
        """
        path = repo.working_dir + '/' + filepath
        with open(path) as fin:
            work = yaml.load(fin, Loader=yaml.FullLoader)

        main_dir = os.getcwd()
        os.chdir(repo.working_dir)

        # Find the jobs to be executed
        for job in work["jobs"]:
            # Skip the build part
            if job["name"] == 'Build project':
                continue
            # Split the tasks to run
            tasks = job["run"].splitlines()
            # Print the current job name
            print("CI Server Test: " + job["name"])

            if job["name"] == 'Lint code':
                # Execute the shell commands
                for task in tasks:
                    # Execute the shell commands
                    result = subprocess.run(task, shell=True, text=True,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    # Return at once if build fails
                    if result.stdout != "":
                        os.chdir(main_dir)
                        return False, result.stdout
            elif job["name"] == 'Run tests':
                for task in tasks:
                    # Execute the shell commands
                    result = subprocess.run(task, shell=True, text=True,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    # Print the output of the shell commands
                    print(result.stdout)
                    # Return at once if build fails (E: File Error F: Failed)
                    if result.returncode != 0:
                        os.chdir(main_dir)
                        return False, result.stderr
            elif job["name"] == 'Install dependencies':
                for task in tasks:
                    # Execute the shell commands
                    result = subprocess.run(task, shell=True, text=True,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
                    # Print the output of the shell commands
                    print(result.stdout)
                    # Return at once if build fails
                    if result.stderr != "":
                        os.chdir(main_dir)
                        return False, result.stderr
            else:
                print("ERROR:", "Unrecognized job name!", file=sys.stderr)

        os.chdir(main_dir)
        return True, "Good News: All is Fine."


def read_token():
    with open('token.yml') as fin:
        data = yaml.load(fin, Loader=yaml.FullLoader)
    return data["TOKEN"]


def run(server_class=HTTPServer, handler_class=CIServer, port=8030):
    """
    Initialize server
    """
    server_address = ('', port)
    server = server_class(server_address, handler_class)
    print("Starting server\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    print('Stopping server\n')


def main():
    with open('config.yml') as fin:
        data = yaml.load(fin, Loader=yaml.FullLoader)
    PORT = data["PORT"]
    run(port=PORT)


if __name__ == '__main__':
    main()
