#!/usr/bin/env python

"""

"""
import json
import os
import shutil
from custodian.vasp.handlers import UnconvergedErrorHandler

from fireworks.utilities.fw_serializers import FWSerializable
from fireworks.core.firework import FireTaskBase, FWAction, FireWork, Workflow
from fireworks.utilities.fw_utilities import get_slug
from mpworks.drones.mp_vaspdrone import MPVaspDrone
from mpworks.dupefinders.dupefinder_vasp import DupeFinderVasp
from mpworks.firetasks.vasp_setup_tasks import SetupUnconvergedHandlerTask
from mpworks.workflows.wf_utils import last_relax, _get_custodian_task, get_loc
from pymatgen import Composition
from pymatgen.io.vaspio.vasp_input import Incar, Poscar, Potcar, Kpoints
from pymatgen.matproj.snl import StructureNL

__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Mar 15, 2013'


class VaspWriterTask(FireTaskBase, FWSerializable):
    """
    Write VASP input files based on the fw_spec
    """

    _fw_name = "Vasp Writer Task"

    def run_task(self, fw_spec):
        Incar.from_dict(fw_spec['vasp']['incar']).write_file('INCAR')
        Poscar.from_dict(fw_spec['vasp']['poscar']).write_file('POSCAR')
        Potcar.from_dict(fw_spec['vasp']['potcar']).write_file('POTCAR')
        Kpoints.from_dict(fw_spec['vasp']['kpoints']).write_file('KPOINTS')


class VaspCopyTask(FireTaskBase, FWSerializable):
    """
    Copy the VASP run directory in 'prev_vasp_dir' to the current dir
    """

    _fw_name = "Vasp Copy Task"

    def __init__(self, parameters=None):
        """
        :param parameters: (dict) Potential keys are 'use_CONTCAR', and 'files'
        """
        parameters = parameters if parameters else {}
        self.update(parameters)  # store the parameters explicitly set by the user

        default_files = ['INCAR', 'POSCAR', 'KPOINTS', 'POTCAR', 'OUTCAR',
                         'vasprun.xml', 'CHGCAR', 'OSZICAR']
        self.files = parameters.get('files', default_files)  # files to move
        self.use_contcar = parameters.get('use_CONTCAR', True)  # whether to move CONTCAR to POSCAR
        if self.use_contcar:
            default_files.append('CONTCAR')

    def run_task(self, fw_spec):
        prev_dir = get_loc(fw_spec['prev_vasp_dir'])

        if '$ALL' in self.files:
            self.files = os.listdir(prev_dir)

        for file in self.files:
            prev_filename = last_relax(os.path.join(prev_dir, file))
            dest_file = 'POSCAR' if file == 'CONTCAR' and self.use_contcar else file
            print 'COPYING', prev_filename, dest_file
            shutil.copy2(prev_filename, dest_file)

        return FWAction(stored_data={'copied_files': self.files})


class VaspToDBTask(FireTaskBase, FWSerializable):
    """
    Enter the VASP run directory in 'prev_vasp_dir' to the database.
    """

    _fw_name = "Vasp to Database Task"

    def __init__(self, parameters=None):
        """
        :param parameters: (dict) Potential keys are 'parse_uniform', 'additional_fields', and 'update_duplicates'
        """
        parameters = parameters if parameters else {}
        self.update(parameters)

        self.parse_uniform = self.get('parse_uniform', False)
        self.additional_fields = self.get('additional_fields', {})
        self.update_duplicates = self.get('update_duplicates', False)

    def run_task(self, fw_spec):
        prev_dir = get_loc(fw_spec['prev_vasp_dir'])
        update_spec = {'prev_vasp_dir': prev_dir, 'prev_task_type': fw_spec['prev_task_type'],
                       'run_tags': fw_spec['run_tags']}
        # get the directory containing the db file
        db_dir = os.environ['DB_LOC']
        db_path = os.path.join(db_dir, 'tasks_db.json')

        with open(db_path) as f:
            db_creds = json.load(f)
            drone = MPVaspDrone(
                host=db_creds['host'], port=db_creds['port'],
                database=db_creds['database'], user=db_creds['admin_user'],
                password=db_creds['admin_password'],
                collection=db_creds['collection'], parse_dos=self.parse_uniform,
                additional_fields=self.additional_fields,
                update_duplicates=self.update_duplicates)
            t_id, d = drone.assimilate(prev_dir)

        mpsnl = d['snl_final'] if 'snl_final' in d else d['snl']
        snlgroup_id = d['snlgroup_id_final'] if 'snlgroup_id_final' in d else d['snlgroup_id']
        update_spec.update({'mpsnl': mpsnl, 'snlgroup_id': snlgroup_id})

        print 'ENTERED task id:', t_id
        stored_data = {'task_id': t_id}
        if d['state'] == 'successful':
            update_spec['analysis'] = d['analysis']
            return FWAction(stored_data=stored_data, update_spec=update_spec)

        # not successful - first test to see if UnconvergedHandler is needed
        output_dir = last_relax(os.path.join(prev_dir, 'vasprun.xml'))
        ueh = UnconvergedErrorHandler(output_filename=output_dir)
        if ueh.check() and 'unconverged_handler' not in fw_spec['run_tags']:
            print 'Unconverged run! Creating dynamic FW...'

            spec = {'prev_vasp_dir': prev_dir, 'prev_task_type': fw_spec['task_type'],
                    'mpsnl': mpsnl, 'snlgroup_id': snlgroup_id,
                    'task_type': fw_spec['prev_task_type'], 'run_tags': list(fw_spec['run_tags']),
                    '_dupefinder': DupeFinderVasp().to_dict(), '_priority': 4}

            snl = StructureNL.from_dict(spec['mpsnl'])
            spec['run_tags'].append('unconverged_handler')

            fws = []
            connections = {}

            f = Composition.from_formula(
                snl.structure.composition.reduced_formula).alphabetical_formula

            fws.append(FireWork(
                [VaspCopyTask({'files': ['INCAR', 'KPOINTS', 'POSCAR', 'POTCAR', 'CONTCAR'],
                               'use_CONTCAR': False}), SetupUnconvergedHandlerTask(),
                 _get_custodian_task(spec)], spec, name=get_slug(f + '--' + spec['task_type']),
                fw_id=-2))

            # insert into DB - GGA static
            spec = {'task_type': 'VASP db insertion', '_allow_fizzled_parents': True,
                    '_priority': 4, '_queueadapter': {'nnodes': 1}}
            spec['run_tags'].append('unconverged_handler')
            fws.append(
                FireWork([VaspToDBTask()], spec, name=get_slug(f + '--' + spec['task_type']),
                         fw_id=-1))
            connections[-2] = -1

            wf = Workflow(fws, connections)

            return FWAction(detours=wf)

        # not successful and not due to convergence problem - DEFUSE
        return FWAction(stored_data=stored_data, defuse_children=True)
