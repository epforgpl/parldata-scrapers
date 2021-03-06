from scrapy.conf import settings
import scrapy.log
from scrapy.log import INFO, DEBUG

import vpapi

import json

import os

from visegrad.utils import chunks


class VisegradApiExport(object):
    parliament = ''
    domain = ''
    user = 'scraper'
    parliament_code = ''
    single_chamber = True
    motions_ids = {}
    events_ids = {}

    PEOPLE_FILE = 'Person.json'
    ORGANIZATIONS_FILE = 'Organization.json'
    MEMBERSHIPS_FILE = 'Membership.json'
    MOTIONS_FILE = 'Motion.json'
    VOTE_EVENTS_FILE = 'VoteEvent.json'
    VOTES_FILE = 'Vote.json'
    EVENTS_FILE = 'Event.json'
    SPEECHES_FILE = 'Speech.json'
    FILES = {
        'people': PEOPLE_FILE,
        'organizations': ORGANIZATIONS_FILE,
        'memberships': MEMBERSHIPS_FILE,
        'motions': MOTIONS_FILE,
        'vote-events': VOTE_EVENTS_FILE,
        'votes': VOTES_FILE,
        'events': EVENTS_FILE,
        'speeches': SPEECHES_FILE,
    }

    def __init__(self, log = None):
        vpapi.parliament(self.get_parliament())
        vpapi.authorize(self.get_user(), self.get_password())

        self._chamber = None
        self._ids = {}
        if log is None:
            self.log = scrapy.log.msg
        else:
            self.log = log

    def get_parliament(self):
        return settings.get('VPAPI_PARLIAMENT_ENDPOINT', self.parliament)

    def get_user(self):
        return self.user

    def get_password(self):
        var = 'VPAPI_PWD_%s' % self.parliament_code.upper()
        return settings.get(var)

    def run_export(self):
        self.log('Exporting people', INFO)
        self.export_people()
        self.log('Exporting organizations', INFO)
        self.export_organizations()
        self.log('Exporting memberships', INFO)
        self.export_memberships()
        self.log('Exporting events', INFO)
        self.export_events()
        self.log('Exporting motions', INFO)
        self.export_motions()
        self.log('Exporting votes', INFO)
        self.export_votes()
        self.log('Exporting speeches', INFO)
        self.export_speeches()

    def load_json(self, source, exclude=None):
        if exclude is None:
            exclude = lambda x: False

        filename = os.path.join(
            settings.get('OUTPUT_PATH', ''),
            self.domain,
            self.FILES[source]
        )
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                for line in f:
                    item = json.loads(line.rstrip())
                    if not exclude(item):
                        yield item

    def get_or_create(self, endpoint, item, refresh=False, where_keys=None):
        sort = []
        embed = []
        where = {}
        if where_keys:
            for key in where_keys:
                where[key] = item[key]
        elif endpoint == 'memberships':
            where = {
                'person_id': item['person_id'],
                'organization_id': item['organization_id']
            }
            where['start_date'] = item.get('start_date', {"$exists": False})

            sort = [('start_date', -1)]
        elif endpoint in ('motions', 'speeches'):
            where = {'sources.url': item['sources'][0]['url']}
        elif endpoint == 'vote-events':
            embed = ['votes']
            if 'motion_id' in item:
                where = {'motion_id': item['motion_id']}
            else:
                where = {'start_date': item['start_date']}
        elif endpoint == 'votes':
            where = {
                'vote_event_id': item['vote_event_id'],
                'voter_id': item['voter_id'],
            }
        elif endpoint == 'events':
            where = {'identifier': item['identifier']}
        else:
            where = {
                'identifiers': {'$elemMatch': item['identifiers'][0]}}
        created = False
        resp = vpapi.getfirst(endpoint, where=where, sort=sort)
        if not resp:
            resp = vpapi.post(endpoint, item)
            created = True
            self.log('Created %s' % resp['_links']['self']['href'], DEBUG)
        else:
            pk = resp['id']
            resp = vpapi.put("%s/%s" % (endpoint, pk), item)
            self.log('Updated %s' % resp['_links']['self']['href'], DEBUG)

        if resp['_status'] != 'OK':
            raise Exception(resp)
        if refresh:
            resp = vpapi.get(
                resp['_links']['self']['href'], sort=sort, embed=embed)
        resp['_created'] = created
        return resp

    def batch_create(self, endpoint, items):
        resp = vpapi.post(endpoint, items)
        if resp['_status'] != 'OK':
            raise Exception(resp)
        self.log('Created %d items' % len(resp['_items']), DEBUG)
        return

    def get_remote_id(self, scheme, identifier):
        key = "%s/%s" % (scheme, identifier)
        if key in self._ids:
            return self._ids[key]

        domain, category = scheme.split('/')
        if category in ('committees', 'parties', 'chamber'):
            endpoint = 'organizations'
        else:
            endpoint = category

        resp = vpapi.get(endpoint, where={
            'identifiers': {
                '$elemMatch': {'scheme': scheme, 'identifier': identifier}
            }
        })

        if resp['_items']:
            item = resp['_items'][0]
            self._ids[key] = item['id']
            return item['id']

    def make_chamber(self, index):
        raise NotImplementedError()

    def get_chamber(self, index=0):
        if not self._chamber:
            self._chamber = self.make_chamber(index)
        return self._chamber

    def export_people(self):
        chamber = self.get_chamber()
        people = self.load_json('people')

        for person in people:
            resp = self.get_or_create('people', person)
            if self.single_chamber:
                membership = {
                    'person_id': resp['id'],
                    'organization_id': chamber['id']
                }
                self.get_or_create('memberships', membership)

    def export_organizations(self):
        chamber = self.get_chamber()
        organizations = self.load_json('organizations')

        for organization in organizations:
            if self.single_chamber and 'parent_id' not in organization:
                organization['parent_id'] = chamber['id']
            elif 'parent_id' in organization:
                organization['parent_id'] = self.get_remote_id(
                    scheme=organization['parent_id']['scheme'],
                    identifier=organization['parent_id']['identifier']
                )
            self.get_or_create('organizations', organization)

    def export_memberships(self):
        memberships = self.load_json('memberships')

        for item in memberships:
            person_id = self.get_remote_id(
                scheme=item['person_id']['scheme'],
                identifier=item['person_id']['identifier'])
            organization_id = self.get_remote_id(
                scheme=item['organization_id']['scheme'],
                identifier=item['organization_id']['identifier'])
            if person_id and organization_id:
                item['person_id'] = person_id
                item['organization_id'] = organization_id
                self.get_or_create('memberships', item)

    def export_events(self):
        chamber = self.get_chamber()
        parent_events = self.load_json(
            'events', exclude=lambda x: 'parent_id' in x)
        child_events = self.load_json(
            'events', exclude=lambda x: 'parent_id' not in x)

        for item in parent_events:
            item['organization_id'] = chamber['id']
            resp = self.get_or_create('events', item)
            self.events_ids[item['identifier']] = resp['id']

        for item in child_events:
            item['organization_id'] = chamber['id']
            item['parent_id'] = self.events_ids[item['parent_id']]
            resp = self.get_or_create('events', item)
            self.events_ids[item['identifier']] = resp['id']

    def export_motions(self):
        chamber = self.get_chamber()
        motions = self.load_json('motions')
        motion_id = None

        for item in motions:
            item['organization_id'] = chamber['id']
            if 'id' in item:
                motion_id = item['id']
                del item['id']
            session_id = item.get('legislative_session_id')
            if session_id:
                item['legislative_session_id'] = self.events_ids[session_id]
            resp = self.get_or_create('motions', item)

            if motion_id:
                self.motions_ids[motion_id] = resp['id']

    def export_votes(self):
        vote_events = self.load_json('vote-events')
        votes = self.load_json('votes')
        vote_events_ids = {}

        for vote_event in vote_events:
            local_identifier = vote_event['identifier']
            del vote_event['identifier']

            if 'motion_id' in vote_event:
                vote_event['motion_id'] = self.motions_ids[vote_event['motion_id']]

            session_id = vote_event.get('legislative_session_id')
            if session_id:
                vote_event['legislative_session_id'] = self.events_ids[session_id]

            vote_event_resp = self.get_or_create(
                'vote-events', vote_event, refresh=True)
            # send votes only once, when vote event is created
            if not vote_event_resp.get('votes'):
                vote_events_ids[local_identifier] = vote_event_resp['id']

        filter_func = lambda x: x['vote_event_id'] in vote_events_ids
        for votes_chunk in chunks(votes, 400, filter_func):
            for v in votes_chunk:
                v['vote_event_id'] = vote_events_ids[v['vote_event_id']]
                v['voter_id'] = self.get_remote_id(
                        scheme=v['voter_id']['scheme'],
                        identifier=v['voter_id']['identifier'])
            self.batch_create('votes', votes_chunk)

    def export_speeches(self):
        speeches = self.load_json('speeches')

        for speech in speeches:
            if 'creator_id' in speech:
                speech['creator_id'] = self.get_remote_id(
                    scheme=speech['creator_id']['scheme'],
                    identifier=speech['creator_id']['identifier'])
            session_id = speech.get('event_id')
            if session_id:
                speech['event_id'] = self.events_ids[session_id]
            self.get_or_create('speeches', speech)
