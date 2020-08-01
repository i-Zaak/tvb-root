# -*- coding: utf-8 -*-
#
#
# TheVirtualBrain-Framework Package. This package holds all Data Management, and 
# Web-UI helpful to run brain-simulations. To use it, you also need do download
# TheVirtualBrain-Scientific Package (for simulators). See content of the
# documentation-folder for more details. See also http://www.thevirtualbrain.org
#
# (c) 2012-2020, Baycrest Centre for Geriatric Care ("Baycrest") and others
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this
# program.  If not, see <http://www.gnu.org/licenses/>.
#
#
#   CITATION:
# When using The Virtual Brain for scientific publications, please cite it as follows:
#
#   Paula Sanz Leon, Stuart A. Knock, M. Marmaduke Woodman, Lia Domide,
#   Jochen Mersmann, Anthony R. McIntosh, Viktor Jirsa (2013)
#       The Virtual Brain: a simulator of primate brain network dynamics.
#   Frontiers in Neuroinformatics (7:10. doi: 10.3389/fninf.2013.00010)
#
#

"""
.. moduleauthor:: Adrian Dordea <adrian.dordea@codemart.ro>
.. moduleauthor:: Lia Domide <lia.domide@codemart.ro>
.. moduleauthor:: Calin Pavel <calin.pavel@codemart.ro>
.. moduleauthor:: Bogdan Neacsa <bogdan.neacsa@codemart.ro>
"""

import os
import shutil
from cgi import FieldStorage
from collections import OrderedDict
from datetime import datetime
from cherrypy._cpreqbody import Part
from sqlalchemy.orm.attributes import manager_of_class
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from tvb.basic.profile import TvbProfile
from tvb.basic.logger.builder import get_logger
from tvb.config import VIEW_MODEL2ADAPTER
from tvb.config.algorithm_categories import UploadAlgorithmCategoryConfig
from tvb.core.entities.file.simulator.burst_configuration_h5 import BurstConfigurationH5
from tvb.core.entities.model.model_datatype import DataTypeGroup
from tvb.core.entities.model.model_operation import ResultFigure, Operation, STATUS_FINISHED
from tvb.core.entities.model.model_project import Project
from tvb.core.entities.storage import dao, transactional
from tvb.core.entities.model.model_burst import BurstConfiguration
from tvb.core.entities.file.xml_metadata_handlers import XMLReader
from tvb.core.entities.file.files_helper import FilesHelper
from tvb.core.entities.file.files_update_manager import FilesUpdateManager
from tvb.core.entities.file.exceptions import FileStructureException, MissingDataSetException
from tvb.core.entities.file.exceptions import IncompatibleFileManagerException
from tvb.core.services.exceptions import ImportException, ServicesBaseException
from tvb.core.services.algorithm_service import AlgorithmService
from tvb.core.project_versions.project_update_manager import ProjectUpdateManager
from tvb.core.neocom import h5
from tvb.core.neocom.h5 import REGISTRY
from tvb.core.neotraits._h5core import H5File, ViewModelH5


class ImportService(object):
    """
    Service for importing TVB entities into system.
    It supports TVB exported H5 files as input, but it should also handle H5 files
    generated outside of TVB, as long as they respect the same structure.
    """

    def __init__(self):
        self.logger = get_logger(__name__)
        self.user_id = None
        self.files_helper = FilesHelper()
        self.created_projects = []

    def _download_and_unpack_project_zip(self, uploaded, uq_file_name, temp_folder):

        if isinstance(uploaded, FieldStorage) or isinstance(uploaded, Part):
            if not uploaded.file:
                raise ImportException("Please select the archive which contains the project structure.")
            with open(uq_file_name, 'wb') as file_obj:
                self.files_helper.copy_file(uploaded.file, file_obj)
        else:
            shutil.copy2(uploaded, uq_file_name)

        try:
            self.files_helper.unpack_zip(uq_file_name, temp_folder)
        except FileStructureException as excep:
            self.logger.exception(excep)
            raise ImportException("Bad ZIP archive provided. A TVB exported project is expected!")

    @staticmethod
    def _compute_unpack_path():
        """
        :return: the name of the folder where to expand uploaded zip
        """
        now = datetime.now()
        date_str = "%d-%d-%d_%d-%d-%d_%d" % (now.year, now.month, now.day, now.hour,
                                             now.minute, now.second, now.microsecond)
        uq_name = "%s-ImportProject" % date_str
        return os.path.join(TvbProfile.current.TVB_TEMP_FOLDER, uq_name)

    @transactional
    def import_project_structure(self, uploaded, user_id):
        """
        Execute import operations:

        1. check if ZIP or folder
        2. find all project nodes
        3. for each project node:
            - create project
            - create all operations
            - import all images
            - create all dataTypes
        """

        self.user_id = user_id
        self.created_projects = []

        # Now compute the name of the folder where to explode uploaded ZIP file
        temp_folder = self._compute_unpack_path()
        uq_file_name = temp_folder + ".zip"

        try:
            self._download_and_unpack_project_zip(uploaded, uq_file_name, temp_folder)
            self._import_projects_from_folder(temp_folder)

        except Exception as excep:
            self.logger.exception("Error encountered during import. Deleting projects created during this operation.")
            # Remove project folders created so far.
            # Note that using the project service to remove the projects will not work,
            # because we do not have support for nested transaction.
            # Removing from DB is not necessary because in transactional env a simple exception throw
            # will erase everything to be inserted.
            for project in self.created_projects:
                project_path = os.path.join(TvbProfile.current.TVB_STORAGE, FilesHelper.PROJECTS_FOLDER, project.name)
                shutil.rmtree(project_path)
            raise ImportException(str(excep))

        finally:
            # Now delete uploaded file
            if os.path.exists(uq_file_name):
                os.remove(uq_file_name)
            # Now delete temporary folder where uploaded ZIP was exploded.
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)

    def _import_projects_from_folder(self, temp_folder):
        """
        Process each project from the uploaded pack, to extract names.
        """
        project_roots = []
        for root, _, files in os.walk(temp_folder):
            if FilesHelper.TVB_PROJECT_FILE in files:
                project_roots.append(root)

        for project_path in project_roots:
            update_manager = ProjectUpdateManager(project_path)
            update_manager.run_all_updates()
            project_entity = self.__populate_project(project_path)

            # Compute the path where to store files of the imported project
            new_project_path = os.path.join(TvbProfile.current.TVB_STORAGE,
                                            FilesHelper.PROJECTS_FOLDER, project_entity.name)
            if project_path != new_project_path:
                shutil.move(project_path, new_project_path)

            self.created_projects.append(project_entity)

            # Now import project operations
            self.import_project_operations(project_entity, new_project_path)

            # Import images
            self._store_imported_images(project_entity)

    @staticmethod
    def _append_tmp_to_folder_containing_operation(path):
        """
        Return the renamed path of the operation folder
        """
        operation_file_path = None
        for root, _, files in os.walk(path):
            if "Operation.xml" in files:
                # Found an operation folder - append TMP to its name
                tmp_op_folder = root + 'tmp'
                os.rename(root, tmp_op_folder)
                operation_file_path = os.path.join(tmp_op_folder, "Operation.xml")
        return operation_file_path

    def _load_operation_from_path(self, project, op_path):
        """
        Load operation from path containing it.
        """
        if op_path:
            operation = self.__build_operation_from_file(project, op_path)
            operation.import_file = op_path
            return operation
        return None

    def _load_datatypes_from_operation_folder(self, op_path, operation_entity, datatype_group):
        """
        Loads datatypes from operation folder
        :returns: Datatypes ordered by creation date (to solve any dependencies)
        """
        all_datatypes = []
        for file_name in os.listdir(op_path):
            if file_name.endswith(FilesHelper.TVB_STORAGE_FILE_EXTENSION):
                h5_file = os.path.join(op_path, file_name)
                try:
                    file_update_manager = FilesUpdateManager()
                    file_update_manager.upgrade_file(h5_file)
                    datatype = self.load_datatype_from_file(op_path, file_name, operation_entity.id, datatype_group)
                    all_datatypes.append(datatype)

                except IncompatibleFileManagerException:
                    os.remove(h5_file)
                    self.logger.warning("Incompatible H5 file will be ignored: %s" % h5_file)
                    self.logger.exception("Incompatibility details ...")

        all_datatypes.sort(key=lambda dt_date: dt_date.create_date)
        for dt in all_datatypes:
            self.logger.debug("Import order %s: %s" % (dt.type, dt.gid))
        return all_datatypes


    def _store_imported_datatypes_in_db(self, project, all_datatypes, dt_burst_mappings, burst_ids_mapping):
        def by_time(dt):
            return dt.create_date or datetime.now()

        if burst_ids_mapping is None:
            burst_ids_mapping = {}
        if dt_burst_mappings is None:
            dt_burst_mappings = {}

        all_datatypes.sort(key=by_time)

        for datatype in all_datatypes:
            old_burst_id = dt_burst_mappings.get(datatype.gid)

            if old_burst_id is not None:
                datatype.fk_parent_burst = burst_ids_mapping[old_burst_id]

            datatype_allready_in_tvb = dao.get_datatype_by_gid(datatype.gid)

            if not datatype_allready_in_tvb:
                self.store_datatype(datatype)
            else:
                AlgorithmService.create_link([datatype_allready_in_tvb.id], project.id)

    def _store_imported_images(self, project):
        """
        Import all images from project
        """
        images_root = self.files_helper.get_images_folder(project.name)
        # for file_name in os.listdir(images_root):
        for root, _, files in os.walk(images_root):
            for file_name in files:
                if file_name.endswith(FilesHelper.TVB_FILE_EXTENSION):
                    self._populate_image(os.path.join(root, file_name), project.id)

    def import_project_operations(self, project, import_path, dt_burst_mappings=None, burst_ids_mapping=None):
        """
        This method scans provided folder and identify all operations that needs to be imported
        """
        operation_paths = self._get_operation_paths(import_path)
        imported_operations = []

        for path in operation_paths:
            view_model, dt_paths = self._get_view_model_and_datatypes_paths(path)
            if not view_model:
                op_path = self._append_tmp_to_folder_containing_operation(path)
                operation = self._load_operation_from_path(project, op_path)

                if operation:
                    self.logger.debug("Importing operation " + str(operation))
                    old_operation_folder, _ = os.path.split(operation.import_file)

                    operation_entity, datatype_group = self.__import_operation(operation)

                    # Rename operation folder with the ID of the stored operation
                    new_operation_path = FilesHelper().get_operation_folder(project.name, operation_entity.id)
                    if old_operation_folder != new_operation_path:
                        # Delete folder of the new operation, otherwise move will fail
                        shutil.rmtree(new_operation_path)
                        shutil.move(old_operation_folder, new_operation_path)

                    operation_datatypes = self._load_datatypes_from_operation_folder(new_operation_path,
                                                                                     operation_entity,
                                                                                     datatype_group)
                    self._store_imported_datatypes_in_db(project, operation_datatypes, dt_burst_mappings,
                                                         burst_ids_mapping)
                    imported_operations.append(operation_entity)
            else:
                start_date = datetime.now()
                alg = VIEW_MODEL2ADAPTER[type(view_model)]

                # import operation only if there is a algolithm
                if alg:
                    op = self.get_new_operation_for_view_model(project, view_model, alg.id)
                    op.meta_data = '{"from": "Import"}'
                    op.status = STATUS_FINISHED
                    op.create_date = start_date
                    op.start_date = start_date
                    op.algorithm = alg
                    op.visible = True
                    op.completion_date = datetime.now()
                    operation_entity = dao.store_entity(op)
                    imported_operations.append(operation_entity)

                    # Store the DataTypes in db
                    if dt_paths:
                        self._store_datatypes_from_path_in_db(dt_paths, project.id, op.id)

        return imported_operations

    @staticmethod
    def _get_operation_paths(paths):
        path_list = [f.path for f in os.scandir(paths) if f.is_dir()]
        sorted_dir = {}
        for path in path_list:
            op_number = os.path.basename(path)
            sorted_dir[int(op_number)] = path

        sorted_dir = OrderedDict(sorted(sorted_dir.items()))

        return list(sorted_dir.values())

    def get_new_operation_for_view_model(self, project, view_model, alg_id):
        op_param = '{"gid": "' + str(view_model.gid) + '"}'
        op = Operation(project.fk_admin, project.id, alg_id, op_param)
        return op

    @staticmethod
    def _get_view_model_and_datatypes_paths(import_path):
        vm = None
        dt_paths = []
        for root, _, files in os.walk(import_path):
            for file in files:
                if file.endswith(".h5"):
                    vm_file_oath = os.path.join(root, file)
                    h5_class = H5File.h5_class_from_file(vm_file_oath)
                    if h5_class is ViewModelH5:
                        if not vm:
                            view_model = h5.load_view_model_from_file(vm_file_oath)
                            if hasattr(view_model, "is_main") and view_model.is_main == True:
                                vm = view_model
                    else:
                        dt_paths.append(vm_file_oath)
        return vm, dt_paths

    @staticmethod
    def _store_datatypes_from_path_in_db(paths, project_id, op_id):
        if paths:
            for path in paths:
                h5_class = H5File.h5_class_from_file(path)
                if h5_class is BurstConfigurationH5:
                    h5_file = BurstConfigurationH5(path)
                    dt = BurstConfiguration(project_id)
                    dt.fk_simulation = op_id
                    h5_file.load_into(dt)
                    dao.store_entity(dt)
                else:
                    dt, generic_attr = h5.load_with_links(path)
                    index = REGISTRY.get_index_for_h5file(h5_class)()
                    index.fill_from_has_traits(dt)
                    index.fill_from_generic_attributes(generic_attr)

                    dao.store_entity(index)

    def _populate_image(self, file_name, project_id):
        """
        Create and store a image entity.
        """
        figure_dict = XMLReader(file_name).read_metadata()
        new_path = os.path.join(os.path.split(file_name)[0], os.path.split(figure_dict['file_path'])[1])
        if not os.path.exists(new_path):
            self.logger.warning("Expected to find image path %s .Skipping" % new_path)

        op = dao.get_operation_by_gid(figure_dict['fk_from_operation'])
        figure_dict['fk_op_id'] = op.id if op is not None else None
        figure_dict['fk_user_id'] = self.user_id
        figure_dict['fk_project_id'] = project_id
        figure_entity = manager_of_class(ResultFigure).new_instance()
        figure_entity = figure_entity.from_dict(figure_dict)
        stored_entity = dao.store_entity(figure_entity)

        # Update image meta-data with the new details after import
        figure = dao.load_figure(stored_entity.id)
        self.logger.debug("Store imported figure")
        self.files_helper.write_image_metadata(figure)

    def load_datatype_from_file(self, storage_folder, file_name, op_id, datatype_group=None,
                                move=True, final_storage=None):
        """
        Creates an instance of datatype from storage / H5 file 
        :returns: DatatypeIndex
        """
        self.logger.debug("Loading DataType from file: %s" % file_name)
        datatype, generic_attributes = h5.load_with_references(os.path.join(storage_folder, file_name))
        index_class = h5.REGISTRY.get_index_for_datatype(datatype.__class__)
        datatype_index = index_class()
        datatype_index.fill_from_has_traits(datatype)
        datatype_index.fill_from_generic_attributes(generic_attributes)

        # Add all the required attributes
        if datatype_group is not None:
            datatype_index.fk_datatype_group = datatype_group.id
        datatype_index.fk_from_operation = op_id

        associated_file = h5.path_for_stored_index(datatype_index)
        if os.path.exists(associated_file):
            datatype_index.disk_size = FilesHelper.compute_size_on_disk(associated_file)

        # Now move storage file into correct folder if necessary
        if move and final_storage is not None:
            current_file = os.path.join(storage_folder, file_name)
            h5_type = h5.REGISTRY.get_h5file_for_datatype(datatype.__class__)
            final_path = h5.path_for(final_storage, h5_type, datatype.gid)
            if final_path != current_file and move:
                shutil.move(current_file, final_path)

        return datatype_index

    def store_datatype(self, datatype):
        """This method stores data type into DB"""
        try:
            self.logger.debug("Store datatype: %s with Gid: %s" % (datatype.__class__.__name__, datatype.gid))
            return dao.store_entity(datatype)
        except MissingDataSetException as e:
            self.logger.exception(e)
            error_msg = "Datatype %s has missing data and could not be imported properly." % (datatype,)
            raise ImportException(error_msg)
        except IntegrityError as excep:
            self.logger.exception(excep)
            error_msg = "Could not import data with gid: %s. There is already a one with " \
                        "the same name or gid." % datatype.gid
            raise ImportException(error_msg)

    def __populate_project(self, project_path):
        """
        Create and store a Project entity.
        """
        self.logger.debug("Creating project from path: %s" % project_path)
        project_dict = self.files_helper.read_project_metadata(project_path)

        project_entity = manager_of_class(Project).new_instance()
        project_entity = project_entity.from_dict(project_dict, self.user_id)

        try:
            self.logger.debug("Storing imported project")
            return dao.store_entity(project_entity)
        except IntegrityError as excep:
            self.logger.exception(excep)
            error_msg = ("Could not import project: %s with gid: %s. There is already a "
                         "project with the same name or gid.") % (project_entity.name, project_entity.gid)
            raise ImportException(error_msg)

    def __build_operation_from_file(self, project, operation_file):
        """
        Create Operation entity from metadata file.
        """
        operation_dict = XMLReader(operation_file).read_metadata()
        operation_entity = manager_of_class(Operation).new_instance()
        return operation_entity.from_dict(operation_dict, dao, self.user_id, project.gid)

    @staticmethod
    def __import_operation(operation_entity):
        """
        Store a Operation entity.
        """
        operation_entity = dao.store_entity(operation_entity)
        operation_group_id = operation_entity.fk_operation_group
        datatype_group = None

        if operation_group_id is not None:
            try:
                datatype_group = dao.get_datatypegroup_by_op_group_id(operation_group_id)
            except SQLAlchemyError:
                # If no dataType group present for current op. group, create it.
                operation_group = dao.get_operationgroup_by_id(operation_group_id)
                datatype_group = DataTypeGroup(operation_group, operation_id=operation_entity.id)
                datatype_group.state = UploadAlgorithmCategoryConfig.defaultdatastate
                datatype_group = dao.store_entity(datatype_group)

        return operation_entity, datatype_group

    def import_simulator_configuration_zip(self, zip_file):
        # Now compute the name of the folder where to explode uploaded ZIP file
        temp_folder = self._compute_unpack_path()
        uq_file_name = temp_folder + ".zip"

        if isinstance(zip_file, FieldStorage) or isinstance(zip_file, Part):
            if not zip_file.file:
                raise ServicesBaseException("Could not process the given ZIP file...")

            with open(uq_file_name, 'wb') as file_obj:
                self.files_helper.copy_file(zip_file.file, file_obj)
        else:
            shutil.copy2(zip_file, uq_file_name)

        try:
            self.files_helper.unpack_zip(uq_file_name, temp_folder)
            return temp_folder
        except FileStructureException as excep:
            raise ServicesBaseException("Could not process the given ZIP file..." + str(excep))
