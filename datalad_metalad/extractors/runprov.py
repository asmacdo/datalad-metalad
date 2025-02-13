# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""MetadataRecord extractor for provenance information in DataLad's `run` records

Concept
-------

- Find all the commits with a run-record encoded in them
- the commit SHA provides @id for the "activity"
- pull out the author/date info for annotation purposes
- pull out the run record (at the very least to report it straight
  up, but there can be more analysis of the input/output specs in
  the context of the repo state at that point)
- pull out the diff: this gives us the filenames and shasums of
  everything touched by the "activity". This info can then be used
  to look up which file was created by which activity and report
  that in the content metadata

Here is a sketch of the reported metadata structure::

    {
      "@context": "http://openprovenance.org/prov.jsonld",
      "@graph": [
        # agents
        {
          "@id": "Name_Surname<email@example.com>",
          "@type": "agent"
        },
        ...
        # activities
        {
          "@id": "<GITSHA_of_run_record>",
          "@type": "activity",
          "atTime": "2019-05-01T12:10:55+02:00",
          "rdfs:comment": "[DATALAD RUNCMD] rm test.png",
          "prov:wasAssociatedWith": {
            "@id": "Name_Surname<email@example.com>",
          }
        },
        ...
        # entities
        {
          "@id": "SOMEKEY",
          "@type": "entity",
          "prov:wasGeneratedBy": {"@id": "<GITSHA_of_run_record>"}
        }
        ...
      ]
    }
"""


from .base import MetadataExtractor
from .. import (
    get_file_id,
    get_agent_id,
)
from six import (
    text_type,
)
from datalad.support.json_py import (
    loads as jsonloads,
    load as jsonload,
)
from datalad.utils import (
    Path,
)

import logging
lgr = logging.getLogger('datalad.metadata.extractors.runprov')


class RunProvenanceExtractor(MetadataExtractor):
    def __call__(self, dataset, refcommit, process_type, status):
        # shortcut
        ds = dataset

        # lookup dict to find an activity that generated a file at a particular
        # path
        path_db = {}
        # all discovered activities indexed by their commit sha
        activities = {}

        for rec in yield_run_records(ds):
            # run records are coming in latest first
            for d in rec.pop('diff', []):
                if d['path'] in path_db:
                    # records are latest first, if we have an entry, we already
                    # know about the latest change
                    continue
                if d['mode'] == '000000':
                    # this file was deleted, hence it cannot possibly be part
                    # of the to-be-described set of files
                    continue
                # record which activity generated this file
                path_db[d['path']] = dict(
                    activity=rec['gitshasum'],
                    # we need to capture the gitshasum of the file as generated
                    # by the activity to be able to discover later modification
                    # between this state and the to-be-described state
                    gitshasum=d['gitshasum'],
                )
            activities[rec['gitshasum']] = rec

        yielded_files = False
        if process_type in ('all', 'content'):
            for rec in status:
                # see if we have any knowledge about this entry
                # from any of the activity change logs
                dbrec = path_db.get(
                    Path(rec['path']).relative_to(ds.pathobj).as_posix(),
                    {})
                if dbrec.get('gitshasum', None) == rec.get('gitshasum', ''):
                    # the file at this path was generated by a recorded
                    # activity
                    yield dict(
                        rec,
                        metadata={
                            '@id': get_file_id(rec),
                            "@type": "entity",
                            "prov:wasGeneratedBy": {
                                "@id": dbrec['activity'],
                            },
                        },
                        type=rec['type'],
                        status='ok',
                    )
                    yielded_files = True
                else:
                    # we don't know an activity that made this file, but we
                    # could still report who has last modified it
                    # no we should not, this is the RUN provenance extractor
                    # this stuff can be done by the core extractor
                    pass

        if process_type in ('all', 'dataset'):
            agents = {}
            graph = []
            for actsha in sorted(activities):
                rec = activities[actsha]
                agent_id = get_agent_id(rec['author_name'], rec['author_email'])
                # do not report docs on agents immediately, but collect them
                # and give unique list at the end
                agents[agent_id] = dict(
                    name=rec['author_name'],
                    email=rec['author_email']
                )
                graph.append({
                    '@id': actsha,
                    '@type': 'activity',
                    'atTime': rec['commit_date'],
                    'prov:wasAssociatedWith': {
                        '@id': agent_id,
                    },
                    # TODO extend message with formatted run record
                    # targeted for human consumption (but consider
                    # possible leakage of information from sidecar
                    # runrecords)
                    'rdfs:comment': rec['message'],
                })
            # and now documents on the committers
            # this is likely a duplicate of a report to be expected by
            # the datalad_core extractor, but over there it is configurable
            # and we want self-contained reports per extractor
            # the redundancy will be eaten by XZ compression
            for agent in sorted(agents):
                rec = agents[agent]
                graph.append({
                    '@id': agent,
                    '@type': 'agent',
                    'name': rec['name'],
                    'email': rec['email'],
                })

            if yielded_files or graph:
                # we either need a context report for file records, or
                # we have something to say about this dataset
                # in general, one will not come without the other
                yield dict(
                    metadata={
                        '@context': 'http://openprovenance.org/prov.jsonld',
                        '@graph': graph,
                    },
                    type='dataset',
                    status='ok',
                )


def yield_run_records(ds):

    def _finalize_record(r):
        msg, rec = _split_record_message(r.pop('body', []))
        r['message'] = msg
        # TODO this can also just be a runrecord ID in which case we need
        # to load the file and report its content
        rec = jsonloads(rec)
        if not isinstance(rec, dict):
            # this is a runinfo file name
            rec = jsonload(
                text_type(ds.pathobj / '.datalad' / 'runinfo' / rec),
                # TODO this should not be necessary, instead jsonload()
                # should be left on auto, and `run` should save compressed
                # files with an appropriate extension
                compressed=True,
            )
        r['run_record'] = rec
        return r

    record = None
    indiff = False
    for line in ds.repo.call_git_items_(
            ['log', '-F',
             '--grep', '=== Do not change lines below ===',
             "--pretty=tformat:%x00%x00record%x00%n%H%x00%aN%x00%aE%x00%aI%n%B%x00%x00diff%x00",
             "--raw", "--no-abbrev"]):
        if line == '\0\0record\0':
            indiff = False
            # fresh record
            if record:
                yield _finalize_record(record)
            record = None
        elif record is None:
            record = dict(zip(
                ('gitshasum', 'author_name', 'author_email', 'commit_date'),
                line.split('\0')
            ))
            record['body'] = []
            record['diff'] = []
        elif line == '\0\0diff\0':
            indiff = True
        elif indiff:
            if not line.startswith(':'):
                continue
            diff = line[1:].split(' ')[:4]
            diff.append(line[line.index('\t') + 1:])
            record['diff'].append(
                dict(zip(
                    ('prev_mode', 'mode', 'prev_gitshasum', 'gitshasum',
                     'path'),
                    diff
                ))
            )
        else:
            record['body'].append(line)
    if record:
        yield _finalize_record(record)


def _split_record_message(lines):
    msg = []
    run = []
    inrec = False
    for line in lines:
        if line == "=== Do not change lines below ===":
            inrec = True
        elif line == "^^^ Do not change lines above ^^^":
            inrec = False
        elif inrec:
            run.append(line)
        else:
            msg.append(line)
    return '\n'.join(msg).strip(), ''.join(run)


# TODO report runrecord directory as content-needed, if configuration wants this
# information to be reported. However, such files might be used to prevent leakage
# of sensitive information....
