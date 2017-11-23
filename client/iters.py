import errno
import logging
import client.img_migrator
import client.rpc_client
import client.docker_migrate_worker
import tool.criu_api
import tool.criu_req
import client.mstats

MIGRATION_MODE_LIVE = "live"
MIGRATION_MODE_RESTART = "restart"
MIGRATION_MODES = (MIGRATION_MODE_LIVE, MIGRATION_MODE_RESTART)
PRE_DUMP_ENABLE = True
PRE_DUMP_AUTO_DETECT = None

def is_live_mode(mode):
    """Check is migration running in live mode"""
    return mode == MIGRATION_MODE_LIVE

class migration_iter_controller(object):
    def __init__(self, ct_id, dst_id, connection, mode):
        self._mode = mode
        self.connection = connection
        self.dest_rpc_caller = client.rpc_client._rpc_client(self.connection.fdrpc)

        logging.info("Locally setting up!")
        self._migrate_worker = client.docker_migrate_worker.docker_lm_worker(ct_id)
        self._migrate_worker.init_src()
        self.fs = self._migrate_worker.get_fs(self.connection.fdfs)
        if not self.fs:
            raise Exception("No fs driver found!")
        self.img = client.img_migrator.lm_docker_img("dump")
        self.criu_connection = tool.criu_api.criu_conn(self.connection.fdmem)
        logging.info("Remote Setting up!")
        self.dest_rpc_caller.setup(ct_id, mode)

    def set_options(self, opts):
        self.__force = opts["force"]
        self.__skip_cpu_check = opts["skip_cpu_check"]
        self.__skip_criu_check = opts["skip_criu_check"]
        self.__pre_dump = opts["pre_dump"]
        self._migrate_worker.set_options(opts)
        self._thost = opts["to"]
        self.fs.set_options(opts)
        if self.img:
            self.img.set_options(opts)
        if self.criu_connection:
            self.criu_connection.set_options(opts)
            self.dest_rpc_caller.set_options(opts)

    def start_live_migration(self):
        self.fs.set_work_dir(self.img.work_dir())
        self.__validate_cpu()
        self.__validate_criu_version
        root_pid = self._migrate_worker.root_task_pid()
        migration_stats = client.mstats.live_stats()
        use_pre_dumps = self.__check_use_pre_dumps()
        migration_stats.handle_start()

        iter_count = 0
        # Step1 : FS migration
        logging.info("FS migration start!")
        fsstats = self.fs.start_migration()
        migration_stats.handle_preliminary(fsstats)

        #Step2 : pre-dump TODO
        while use_pre_dumps:
            logging.info("Pre-Dump Docker Container!itersCount:%d",iter_count)
            self.dest_rpc_caller.start_iter(False)
            self.img.new_image_dir()
            self._migrate_worker.pre_dump(root_pid, self.img, self.fs)
            self.img.sync_imgs_to_target(self.dest_rpc_caller,
			                                      self._migrate_worker, self.connection.fdmem, self._thost,True)
            logging.info("checkpoint image migration time:%s",self.img.sync_time)
            self.fs.mnt_diff_sync(self._migrate_worker)
            iter_count += 1
            if iter_count >5:
                break
            self.dest_rpc_caller.end_iter()
        #Step3 : Stop and Copy ,Final dump and restore!
        logging.info("Final dump! ")
        self.dest_rpc_caller.start_iter(False)
        self.img.new_image_dir()
        self._migrate_worker.final_dump(root_pid, self.img, self.fs)
        self.dest_rpc_caller.end_iter()
        #Step4 :Restore and CLean!
        try:
			# Handle final FS and images sync on frozen htype
            logging.info("Final FS and images sync")
            fsstats = self.fs.stop_migration(self._migrate_worker)
            self.img.sync_imgs_to_target(self.dest_rpc_caller,
            		                           self._migrate_worker, self.connection.fdmem, self._thost, False)
            
            self.fs.mnt_diff_sync(self._migrate_worker)
			# Restore htype on target
            logging.info("Asking target host to restore")
            self.dest_rpc_caller.restore_from_images(self._migrate_worker._ct_id,
			                                                  self._migrate_worker.get_ck_dir())

        except Exception:
            self._migrate_worker.migration_fail(self.fs)
            raise

		# Restored on target, can't fail starting from this point
        try:

            dstats = tool.criu_api.criu_get_dstats(self.img)
            migration_stats.handle_iteration(dstats, fsstats)
            logging.info("Migration succeeded")
            self._migrate_worker.migration_complete(self.fs, self.dest_rpc_caller)
            migration_stats.handle_stop(self)
            self.criu_connection.close()
            self.img.close()

        except Exception as e:
            logging.warning("Exception during final cleanup: %s", e)


    def __check_support_mem_track(self):
        req = criu_req.make_dirty_tracking_req(self.img)
        resp = self.criu_connection.send_req(req)
        if not resp.success:
            raise Exception()
        if not resp.HasField('features'):
            return False
        if not resp.features.HasField('mem_track'):
            return False
        return resp.features.mem_track

    def __check_use_pre_dumps(self):
        logging.info("Checking for Dirty Tracking")
        use_pre_dumps = False
        if self.__pre_dump == PRE_DUMP_AUTO_DETECT:
            try:
                # Detect is memory tracking supported
                use_pre_dumps = (self.__check_support_mem_track() and
                                 self.htype.can_pre_dump())
                logging.info("\t`- Auto %s",
                             (use_pre_dumps and "enabled" or "disabled"))
            except Exception:
            # Memory tracking auto detection not supported
                use_pre_dumps = False
                logging.info("\t`- Auto detection not possible - Disabled")
        else:
            use_pre_dumps = self.__pre_dump
            logging.info("\t`- Explicitly %s",
                         (use_pre_dumps and "enabled" or "disabled"))
        self.criu_connection.memory_tracking(use_pre_dumps)
        return use_pre_dumps

    def __validate_cpu(self):
        if self.__skip_cpu_check or self.__force:
            return
        logging.info("Checking CPU compatibility")

        logging.info("\t`- Dumping CPU info")
        req = tool.criu_req.make_cpuinfo_dump_req(self.img)
        resp = self.criu_connection.send_req(req)
        if resp.HasField('cr_errno') and (resp.cr_errno == errno.ENOTSUP):
            logging.info("\t`- Dumping CPU info not supported")
            self.__force = True
            return
        if not resp.success:
            raise Exception("Can't dump cpuinfo")

        logging.info("\t`- Sending CPU info")
        self.img.send_cpuinfo(self.dest_rpc_caller, self.connection.fdmem)
        logging.info("\t`- Checking CPU info")
        if not self.dest_rpc_caller.check_cpuinfo():
            raise Exception("CPUs mismatch")

    def __validate_criu_version(self):
        if self.__skip_criu_check or self.__force:
            return
        logging.info("Checking criu version")
        version = criu_api.get_criu_version()
        if not version:
            raise Exception("Can't get criu version")
        if not self.dest_rpc_caller.check_criu_version(version):
            raise Exception("Incompatible criu versions")
