from argparse import Namespace
from logging import getLogger

import osc.conf
import osc.core
import requests
from openqabot.errors import NoResultsError
from pprint import pformat

from . import QEM_DASHBOARD
from .loader.qem import (
    IncReq,
    JobAggr,
    get_incidents,
    get_incident_results,
    get_aggregate_results,
)
from .types.incident import Incident
from .osclib.comments import CommentAPI
from .openqa import openQAInterface

logger = getLogger("bot.commenter")


class Commenter:
    def __init__(self, args: Namespace) -> None:
        self.dry = args.dry
        self.token = {"Authorization": "Token {}".format(args.token)}
        self.client = openQAInterface(args.openqa_instance)
        self.incidents = get_incidents(self.token)
        self.apiurl = "https://api.suse.de"
        osc.conf.get_config(override_apiurl=self.apiurl)
        self.commentapi = CommentAPI(self.apiurl)

    def __call__(self) -> int:

        logger.info("Start commenting incidents in IBS")

        for inc in self.incidents:
            try:
                i_jobs = get_incident_results(inc.id, self.token)
                u_jobs = get_aggregate_results(inc.id, self.token)
            except ValueError as e:
                logger.debug(e)
                continue
            except NoResultsError as e:
                logger.debug(e)
                continue

            state = "none"
            if any(j["status"] in ["running"] for j in i_jobs + u_jobs):
                logger.info("%s needs to wait a bit longer" % inc)
            else:
                if any(
                    j["status"] not in ["passed", "softfailed"] for j in i_jobs + u_jobs
                ):
                    logger.info("There is a failed job for %s" % inc)
                    state = "failed"
                else:
                    state = "passed"

            msg = self.summarize_message(i_jobs + u_jobs)
            self.osc_comment(inc, msg, state)

        return 0

    def osc_comment(self, inc: Incident, msg: str, state: str) -> None:
        if inc.rr is None:
            logger.debug("Skipping comment -- no request defined")
            return

        if not msg:
            logger.debug("Skipping empty comment")
            return

        kw = {}
        kw["request_id"] = str(inc.rr)

        bot_name = "openqa"
        info = {}
        info["state"] = state
        for key in inc.revisions.keys():
            info["revision_%s_%s" % (key.version, key.arch)] = inc.revisions[key]

        msg = self.commentapi.add_marker(msg, bot_name, info)
        msg = self.commentapi.truncate(msg.strip())

        comments = self.commentapi.get_comments(**kw)
        comment, _ = self.commentapi.comment_find(comments, bot_name, info)

        # To prevent spam, assume same state/result
        # and number of lines in message is a duplicate message
        if comment is not None and comment["comment"].count("\n") == msg.count("\n"):
            logger.debug("Previous comment is too similar")
            return

        if comment is None:
            logger.debug("No comment with this state, looking without the state filter")
            comment, _ = self.commentapi.comment_find(comments, bot_name)

        if comment is None:
            logger.debug("No comment to replace found")
        else:
            if not self.dry:
                self.commentapi.delete(comment["id"])
            else:
                logger.info("Would delete comment %d" % int(comment["id"]))

        if not self.dry:
            self.commentapi.add_comment(comment=msg, **kw)
        else:
            logger.info("Would write comment to request %s" % inc)
            logger.debug(pformat(msg))

    def summarize_message(self, jobs) -> str:
        groups = {}
        for job in jobs:
            if "job_group" not in job:
                # workaround for experimets of some QAM devs
                logger.warning(f"group missing in {job['job_id']}")
                continue
            gl = "{!s}@{!s}".format(
                Commenter.emd(job["job_group"]), Commenter.emd(job["flavor"])
            )
            if gl not in groups:
                groupurl = osc.core.makeurl(
                    self.client.openqa.baseurl,
                    ["tests", "overview"],
                    {
                        "version": job["version"],
                        "groupid": job["group_id"],
                        "flavor": job["flavor"],
                        "distri": job["distri"],
                        "build": job["build"],
                    },
                )
                groups[gl] = {
                    "title": "__Group [{!s}]({!s})__\n".format(gl, groupurl),
                    "passed": 0,
                    "unfinished": 0,
                    "failed": [],
                }

            job_summary = self.__summarize_one_openqa_job(job)
            if job_summary is None:
                groups[gl]["unfinished"] = groups[gl]["unfinished"] + 1
                continue

            # None vs ''
            if not len(job_summary):
                groups[gl]["passed"] = groups[gl]["passed"] + 1
                continue

            # if there is something to report, hold the request
            groups[gl]["failed"].append(job_summary)

        msg = ""
        for group in sorted(groups.keys()):
            msg += "\n\n" + groups[group]["title"]
            infos = []
            if groups[group]["passed"]:
                infos.append("{:d} tests passed".format(groups[group]["passed"]))
            if len(groups[group]["failed"]):
                infos.append("{:d} tests failed".format(len(groups[group]["failed"])))
            if groups[group]["unfinished"]:
                infos.append(
                    "{:d} unfinished tests".format(groups[group]["unfinished"])
                )
            msg += "(" + ", ".join(infos) + ")\n"
            for fail in groups[group]["failed"]:
                msg += fail
        return msg.rstrip("\n")

    @staticmethod
    def emd(string: str) -> str:
        return string.replace("_", r"\_")

    def __summarize_one_openqa_job(self, job) -> str:
        testurl = osc.core.makeurl(
            self.client.openqa.baseurl, ["tests", str(job["job_id"])]
        )
        if not job["status"] in ["passed", "failed", "softfailed"]:
            rstring = job["status"]
            if rstring == "none":
                return None
            return "\n- [{!s}]({!s}) is {!s}".format(job["name"], testurl, rstring)

        if job["status"] == "failed":  # rare case: fail without module fails
            return "\n- [{!s}]({!s}) failed".format(job["name"], testurl)
        return ""
