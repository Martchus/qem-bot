# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
import concurrent.futures as CT
from logging import getLogger
from typing import Any, List, Set, Dict

import urllib3
import urllib3.exceptions

import re

import osc.conf
import osc.core
from osc.util.xml import xml_parse
import xml.etree.ElementTree as ET

from .. import GITEA, OBS_URL
#from ..utils import retry10 as requests
import requests

# FIXME: remove debugging imports/code
import json
from pprint import pprint

log = getLogger("bot.loader.gitea")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_json(query: str, token: Dict[str, str], host: str = GITEA) -> dict:
    try:
        return requests.get(host + "/api/v1/" + query, verify=False, headers=token).json()
    except Exception as e:
        log.exception(e)
        raise e

def get_open_prs(token: Dict[str, str]) -> List[Any]:
    # FIXME: strip down this json file and put it into the testsuite
    with open('pulls.json', 'r', encoding ='utf8') as json_file:
        open_prs = json.loads(json_file.read())
    # FIXME: don't hardcode "SLFO" and do the call for real (pulls.json is based on the response from 2025-05-09)
    # FIXME: use paging parameters to get all of them
    #open_prs = get_json("repos/products/SLFO/pulls?state=open", token)  # https://docs.gitea.com/api/1.20/#tag/repository/operation/repoListPullRequests
    log.info("Loaded %s active incidents", len(open_prs))
    return open_prs


def add_reviews(incident: Dict[str, Any], reviews: List[Any]):
    # FIXME: assign something meaningful here
    # FIXME: what is the distinction between the two fields?
    incident['approved'] = len(reviews) > 0
    incident['inReview'] = incident['inReviewQAM'] = len(reviews) > 0


def add_build_results(incident: Dict[str, Any], obs_urls: List[str]):
    packages = []
    for url in obs_urls:
        project_match = re.search('.*/project/show/(.*)', url)
        if project_match:
            build_info_url = osc.core.makeurl(OBS_URL, ['build', project_match.group(1), '_result'])
            #build_info = xml_parse(osc.core.http_GET(build_info_url))
            # FIXME: strip down and put into the testsute, then comment-in real request
            build_info = ET.parse('build-results-124-' + project_match.group(1) + ".xml")
            #build_info.write('build-results-124-' + project_match.group(1) + ".xml")
            for res in build_info.getroot().findall('result'):
                state = res.get('state')
                if state != 'published':
                    continue
                for status in res.findall('status'):
                    code = status.get('code')
                    if code == 'excluded':
                        continue
                    # FIXME: check whether build was successful
                    packages.append(status.get('package'))
    incident['packages'] = packages


def add_comments(incident: Dict[str, Any], comments: List[Any]):
    # consider all URLs in the most recent comment by autogits_obs_staging_bot
    # FIXME: Use https://src.suse.de/products/SLFO/src/branch/1.1.99/staging.config instead of comment-parsing
    #        to find the relevant project on OBS.
    for comment in reversed(comments):
        body = comment['body']
        user_name = comment['user']['username']
        if user_name == 'autogits_obs_staging_bot':
            add_build_results(incident, re.findall('https://[^ ]*', body))
            break
    pass


def make_incident_from_pr(pr: Dict[str, Any], token: Dict[str, str]):
    log.info("Getting info about PR %s from Gitea", pr.get('number', '?'))
    try:
        number = pr['number']
        repo = pr['base']['repo']
        repo_name = repo['full_name']
        # https://docs.gitea.com/api/1.20/#tag/repository/operation/repoDeletePullReviewRequests
        reviews_url = 'repos/%s/pulls/%s/reviews' % (repo_name, number)
        # https://docs.gitea.com/api/1.20/#tag/issue/operation/issueRemoveIssueBlocking
        comments_url = 'repos/%s/issues/%s/comments' % (repo_name, number)
        incident = {
            'number': number,
            'project': repo['name'],
            'emu': False, # FIXME: what is that?
            'isActive': pr['state'] == 'open',
            'inReviewQAM': False,
            'inReview': False,
            'approved': False,
            'embargoed': False, # FIXME: where to get that from?
            'priority': 0, # FIXME: is there a prio for Gitea PRs in the new workflow?
            'rr_number': None, # FIXME: this is used to render the link to OBS (`${this.appConfig.obsUrl}/request/show/${this.incident.rr_number}`)
            'packages': [], # FIXME: we need to read those from OBS buildinfo and skip the PR unless we have that as an incident needs to have at least one package
            'channels': ['bar'], # FIXME: those seem to be repos, also to be read from OBS
        }
        if number == 124: # FIXME: remove this condition
            # FIXME: strip down and put into the testsute, then comment-in real request
            #reviews = get_json(reviews_url, token)
            with open('reviews-124.json', 'r', encoding ='utf8') as json_file:
                reviews = json.loads(json_file.read())
            #comments = get_json(comments_url, token)
            with open('comments-124.json', 'r', encoding ='utf8') as json_file:
                comments = json.loads(json_file.read())
            add_reviews(incident, reviews)
            add_comments(incident, comments)
        if len(incident['packages']) == 0:
            log.info("Skipping PR %s, no packages found", number)
            return None
        if len(incident['channels']) == 0:
            log.info("Skipping PR %s, no channels found", number)
            return None

    except Exception as e:  # pylint: disable=broad-except
        log.error("Unable to process PR %s", pr.get('number', '?'))
        log.exception(e)
        return None
    return incident


def get_incidents_from_open_prs(open_prs: Set[int], token: Dict[str, str]) -> List[Any]:
    incidents = []

    # configure osc to be able to request build info from OBS
    osc.conf.get_config(override_apiurl=OBS_URL)

    with CT.ThreadPoolExecutor() as executor:
        future_inc = [executor.submit(make_incident_from_pr, pr, token) for pr in open_prs]
        for future in CT.as_completed(future_inc):
            incidents.append(future.result())

    incidents = [inc for inc in incidents if inc]
    return incidents
