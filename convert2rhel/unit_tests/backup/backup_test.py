__metaclass__ = type

import os

import pytest
import six

from convert2rhel import backup, exceptions, repo, unit_tests, utils
from convert2rhel.unit_tests import DownloadPkgMocked, ErrorOnRestoreRestorable, MinimalRestorable, RunSubprocessMocked
from convert2rhel.unit_tests.conftest import centos8


six.add_move(six.MovedModule("mock", "mock", "unittest.mock"))
from six.moves import mock


@pytest.fixture
def run_subprocess_with_empty_rpmdb(monkeypatch, tmpdir):
    """When we use rpm, inject our fake rpmdb instead of the system one."""
    rpmdb = os.path.join(str(tmpdir), "rpmdb")
    os.mkdir(rpmdb)

    class RunSubprocessWithEmptyRpmdb(RunSubprocessMocked):
        def __call__(self, *args, **kwargs):
            # Call the super class for recordkeeping (update how we were
            # called)
            super(RunSubprocessWithEmptyRpmdb, self).__call__(*args, **kwargs)

            if args[0][0] == "rpm":
                args[0].extend(["--dbpath", rpmdb])

            return real_run_subprocess(*args, **kwargs)

    real_run_subprocess = utils.run_subprocess
    instrumented_run_subprocess = RunSubprocessWithEmptyRpmdb()
    monkeypatch.setattr(utils, "run_subprocess", instrumented_run_subprocess)

    return instrumented_run_subprocess


class TestRemovePkgs:
    def test_remove_pkgs_without_backup(self, monkeypatch):
        monkeypatch.setattr(backup.changed_pkgs_control, "backup_and_track_removed_pkg", mock.Mock())
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())
        pkgs = ["pkg1", "pkg2", "pkg3"]

        backup.remove_pkgs(pkgs, False)

        assert backup.changed_pkgs_control.backup_and_track_removed_pkg.call_count == 0
        assert backup.run_subprocess.call_count == len(pkgs)

        rpm_remove_cmd = ["rpm", "-e", "--nodeps"]
        for cmd, pkg in zip(backup.run_subprocess.cmds, pkgs):
            assert rpm_remove_cmd + [pkg] == cmd

    def test_remove_pkgs_with_backup(self, monkeypatch):
        monkeypatch.setattr(backup.changed_pkgs_control, "backup_and_track_removed_pkg", mock.Mock())
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())
        pkgs = ["pkg1", "pkg2", "pkg3"]

        backup.remove_pkgs(pkgs)

        assert backup.changed_pkgs_control.backup_and_track_removed_pkg.call_count == len(pkgs)
        assert backup.run_subprocess.call_count == len(pkgs)
        rpm_remove_cmd = ["rpm", "-e", "--nodeps"]
        for cmd, pkg in zip(backup.run_subprocess.cmds, pkgs):
            assert rpm_remove_cmd + [pkg] == cmd

    @pytest.mark.parametrize(
        ("pkgs_to_remove", "ret_code", "backup_pkg", "critical", "expected"),
        (
            (["pkg1"], 1, False, True, "Error: Couldn't remove {0}."),
            (["pkg1"], 1, False, False, "Couldn't remove {0}."),
        ),
    )
    def test_remove_pkgs_failed_to_remove(
        self,
        pkgs_to_remove,
        ret_code,
        backup_pkg,
        critical,
        expected,
        monkeypatch,
        caplog,
    ):
        run_subprocess_mock = RunSubprocessMocked(
            side_effect=unit_tests.run_subprocess_side_effect(
                (("rpm", "-e", "--nodeps", pkgs_to_remove[0]), ("test", ret_code)),
            )
        )
        monkeypatch.setattr(
            backup,
            "run_subprocess",
            value=run_subprocess_mock,
        )

        if critical:
            with pytest.raises(exceptions.CriticalError):
                backup.remove_pkgs(
                    pkgs_to_remove=pkgs_to_remove,
                    backup=backup_pkg,
                    critical=critical,
                )
        else:
            backup.remove_pkgs(pkgs_to_remove=pkgs_to_remove, backup=backup_pkg, critical=critical)

        assert expected.format(pkgs_to_remove[0]) in caplog.records[-1].message

    def test_remove_pkgs_with_empty_list(self, caplog):
        backup.remove_pkgs([])
        assert "No package to remove" in caplog.messages[-1]


class TestChangedPkgsControlInstallLocalRPMS:
    def test_install_local_rpms_with_empty_list(self, monkeypatch):
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())

        backup.changed_pkgs_control._install_local_rpms([])

        assert backup.run_subprocess.call_count == 0

    def test_install_local_rpms_without_replace(self, monkeypatch):
        monkeypatch.setattr(backup.changed_pkgs_control, "track_installed_pkg", mock.Mock())
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())
        pkgs = ["pkg1", "pkg2", "pkg3"]

        backup.changed_pkgs_control._install_local_rpms(pkgs)

        assert backup.changed_pkgs_control.track_installed_pkg.call_count == len(pkgs)
        assert backup.run_subprocess.call_count == 1
        assert ["rpm", "-i", "pkg1", "pkg2", "pkg3"] == backup.run_subprocess.cmd

    def test_install_local_rpms_with_replace(self, monkeypatch):
        monkeypatch.setattr(backup.changed_pkgs_control, "track_installed_pkg", mock.Mock())
        monkeypatch.setattr(backup, "run_subprocess", RunSubprocessMocked())
        pkgs = ["pkg1", "pkg2", "pkg3"]

        backup.changed_pkgs_control._install_local_rpms(pkgs, replace=True)

        assert backup.changed_pkgs_control.track_installed_pkg.call_count == len(pkgs)
        assert backup.run_subprocess.call_count == 1
        assert ["rpm", "-i", "--replacepkgs", "pkg1", "pkg2", "pkg3"] == backup.run_subprocess.cmd


def test_backup_and_track_removed_pkg(monkeypatch):
    monkeypatch.setattr(backup.RestorablePackage, "backup", mock.Mock())

    control = backup.ChangedRPMPackagesController()
    pkgs = ["pkg1", "pkg2", "pkg3"]
    for pkg in pkgs:
        control.backup_and_track_removed_pkg(pkg)

    assert backup.RestorablePackage.backup.call_count == len(pkgs)
    assert len(control.removed_pkgs) == len(pkgs)


def test_track_installed_pkg():
    control = backup.ChangedRPMPackagesController()
    pkgs = ["pkg1", "pkg2", "pkg3"]
    for pkg in pkgs:
        control.track_installed_pkg(pkg)
    assert control.installed_pkgs == pkgs


def test_track_installed_pkgs():
    control = backup.ChangedRPMPackagesController()
    pkgs = ["pkg1", "pkg2", "pkg3"]
    control.track_installed_pkgs(pkgs)
    assert control.installed_pkgs == pkgs


def test_changed_pkgs_control_remove_installed_pkgs(monkeypatch, caplog):
    removed_pkgs = ["pkg_1"]
    run_subprocess_mock = RunSubprocessMocked(
        side_effect=unit_tests.run_subprocess_side_effect(
            (("rpm", "-e", "--nodeps", removed_pkgs[0]), ("test", 0)),
        )
    )
    monkeypatch.setattr(
        backup,
        "run_subprocess",
        value=run_subprocess_mock,
    )

    control = backup.ChangedRPMPackagesController()
    control.installed_pkgs = removed_pkgs
    control._remove_installed_pkgs()
    assert "Removing package: %s" % removed_pkgs[0] in caplog.records[-1].message


def test_changed_pkgs_control_install_removed_pkgs(monkeypatch):
    install_local_rpms_mock = mock.Mock()
    removed_pkgs = [mock.Mock()]
    monkeypatch.setattr(
        backup.changed_pkgs_control,
        "_install_local_rpms",
        value=install_local_rpms_mock,
    )
    backup.changed_pkgs_control.removed_pkgs = removed_pkgs
    backup.changed_pkgs_control._install_removed_pkgs()
    assert install_local_rpms_mock.call_count == 1


def test_changed_pkgs_control_install_removed_pkgs_without_path(monkeypatch, caplog):
    install_local_rpms_mock = mock.Mock()
    removed_pkgs = [mock.Mock()]
    monkeypatch.setattr(
        backup.changed_pkgs_control,
        "_install_local_rpms",
        value=install_local_rpms_mock,
    )
    backup.changed_pkgs_control.removed_pkgs = removed_pkgs
    backup.changed_pkgs_control.removed_pkgs[0].path = None
    backup.changed_pkgs_control._install_removed_pkgs()
    assert install_local_rpms_mock.call_count == 1
    assert "Couldn't find a backup" in caplog.records[-1].message


def test_changed_pkgs_control_restore_pkgs(monkeypatch):
    install_local_rpms_mock = mock.Mock()
    remove_pkgs_mock = mock.Mock()
    monkeypatch.setattr(
        backup.changed_pkgs_control,
        "_install_local_rpms",
        value=install_local_rpms_mock,
    )
    monkeypatch.setattr(backup, "remove_pkgs", value=remove_pkgs_mock)

    backup.changed_pkgs_control.restore_pkgs()
    assert install_local_rpms_mock.call_count == 1
    assert remove_pkgs_mock.call_count == 1


@centos8
def test_restorable_package_backup(pretend_os, monkeypatch, tmpdir):
    backup_dir = str(tmpdir)
    data_dir = str(tmpdir.join("data-dir"))
    dowloaded_pkg_dir = str(tmpdir.join("some-path"))
    download_pkg_mock = DownloadPkgMocked(return_value=dowloaded_pkg_dir)
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(repo, "DATA_DIR", data_dir)
    monkeypatch.setattr(backup, "download_pkg", download_pkg_mock)
    rp = backup.RestorablePackage(pkgname="pkg-1")
    rp.backup()

    assert download_pkg_mock.call_count == 1
    assert rp.path == dowloaded_pkg_dir


def test_restorable_package_backup_without_dir(monkeypatch, tmpdir, caplog):
    backup_dir = str(tmpdir.join("non-existing"))
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    rp = backup.RestorablePackage(pkgname="pkg-1")
    rp.backup()

    assert "Can't access %s" % backup_dir in caplog.records[-1].message


def test_changedrpms_packages_controller_install_local_rpms(monkeypatch, caplog):
    pkgs = ["pkg-1"]
    run_subprocess_mock = RunSubprocessMocked(
        side_effect=unit_tests.run_subprocess_side_effect(
            (("rpm", "-i", pkgs[0]), ("test", 1)),
        )
    )
    monkeypatch.setattr(
        backup,
        "run_subprocess",
        value=run_subprocess_mock,
    )

    control = backup.ChangedRPMPackagesController()
    result = control._install_local_rpms(pkgs_to_install=pkgs, replace=False, critical=False)

    assert result == False
    assert run_subprocess_mock.call_count == 1
    assert "Couldn't install %s packages." % pkgs[0] in caplog.records[-1].message


def test_changedrpms_packages_controller_install_local_rpms_system_exit(monkeypatch, caplog):
    pkgs = ["pkg-1"]
    run_subprocess_mock = RunSubprocessMocked(
        side_effect=unit_tests.run_subprocess_side_effect(
            (("rpm", "-i", pkgs[0]), ("test", 1)),
        )
    )
    monkeypatch.setattr(
        backup,
        "run_subprocess",
        value=run_subprocess_mock,
    )

    control = backup.ChangedRPMPackagesController()
    with pytest.raises(exceptions.CriticalError):
        control._install_local_rpms(pkgs_to_install=pkgs, replace=False, critical=True)

    assert run_subprocess_mock.call_count == 1
    assert "Error: Couldn't install %s packages." % pkgs[0] in caplog.records[-1].message


@pytest.mark.parametrize(
    ("is_eus_system", "has_internet_access"),
    ((True, True), (False, False), (True, False), (False, True)),
)
@centos8
def test_restorable_package_backup(pretend_os, is_eus_system, has_internet_access, tmpdir, monkeypatch):
    pkg_to_backup = "pkg-1"

    # Python 2.7 needs a string or buffer and not a LocalPath
    tmpdir = str(tmpdir)
    download_pkg_mock = DownloadPkgMocked()
    monkeypatch.setattr(backup, "download_pkg", value=download_pkg_mock)
    monkeypatch.setattr(backup, "BACKUP_DIR", value=tmpdir)
    monkeypatch.setattr(backup.system_info, "corresponds_to_rhel_eus_release", value=lambda: is_eus_system)
    monkeypatch.setattr(backup, "get_hardcoded_repofiles_dir", value=lambda: tmpdir if is_eus_system else None)
    backup.system_info.has_internet_access = has_internet_access

    rp = backup.RestorablePackage(pkgname=pkg_to_backup)
    rp.backup()
    assert download_pkg_mock.call_count == 1


@pytest.fixture
def backup_controller():
    return backup.BackupController()


class TestBackupController:
    def test_push(self, backup_controller, restorable):
        backup_controller.push(restorable)

        assert restorable.called["enable"] == 1
        assert restorable in backup_controller._restorables

    def test_push_invalid(self, backup_controller):
        with pytest.raises(TypeError, match="`1` is not a RestorableChange object"):
            backup_controller.push(1)

    def test_pop(self, backup_controller, restorable):
        backup_controller.push(restorable)
        popped_restorable = backup_controller.pop()

        assert popped_restorable is restorable
        assert restorable.called["restore"] == 1

    def test_pop_multiple(self, backup_controller):
        restorable1 = MinimalRestorable()
        restorable2 = MinimalRestorable()
        restorable3 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(restorable2)
        backup_controller.push(restorable3)

        popped_restorable3 = backup_controller.pop()
        popped_restorable2 = backup_controller.pop()
        popped_restorable1 = backup_controller.pop()

        assert popped_restorable1 is restorable1
        assert popped_restorable2 is restorable2
        assert popped_restorable3 is restorable3

        assert restorable1.called["restore"] == 1
        assert restorable2.called["restore"] == 1
        assert restorable3.called["restore"] == 1

    def test_pop_when_empty(self, backup_controller):
        with pytest.raises(IndexError, match="No backups to restore"):
            backup_controller.pop()

    def test_pop_all(self, backup_controller):
        restorable1 = MinimalRestorable()
        restorable2 = MinimalRestorable()
        restorable3 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(restorable2)
        backup_controller.push(restorable3)

        restorables = backup_controller.pop_all()

        assert len(restorables) == 3
        assert restorables[0] is restorable3
        assert restorables[1] is restorable2
        assert restorables[2] is restorable1

        assert restorable1.called["restore"] == 1
        assert restorable2.called["restore"] == 1
        assert restorable3.called["restore"] == 1

    def test_ready_to_push_after_pop_all(self, backup_controller):
        restorable1 = MinimalRestorable()
        restorable2 = MinimalRestorable()

        backup_controller.push(restorable1)
        popped_restorables = backup_controller.pop_all()
        backup_controller.push(restorable2)

        assert len(popped_restorables) == 1
        assert popped_restorables[0] == restorable1
        assert len(backup_controller._restorables) == 1
        assert backup_controller._restorables[0] is restorable2

    def test_pop_all_when_empty(self, backup_controller):
        with pytest.raises(IndexError, match="No backups to restore"):
            backup_controller.pop_all()

    def test_pop_all_error_in_restore(self, backup_controller, caplog):
        restorable1 = MinimalRestorable()
        restorable2 = ErrorOnRestoreRestorable(exception=ValueError("Restorable2 failed"))
        restorable3 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(restorable2)
        backup_controller.push(restorable3)

        popped_restorables = backup_controller.pop_all()

        assert len(popped_restorables) == 3
        assert popped_restorables == [restorable3, restorable2, restorable1]
        assert caplog.records[-1].message == "Error while rolling back a ErrorOnRestoreRestorable: Restorable2 failed"

    # The following tests are for the 1.4 kludge to split restoration via
    # backup_controller into two parts.  They can be removed once we have
    # all rollback items ported to use the BackupController and the partition
    # code is removed.

    def test_pop_with_partition(self, backup_controller):
        restorable1 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(backup_controller.partition)

        restorable = backup_controller.pop()

        assert restorable == restorable1
        assert backup_controller._restorables == []

    def test_pop_all_with_partition(self, backup_controller):
        restorable1 = MinimalRestorable()
        restorable2 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(backup_controller.partition)
        backup_controller.push(restorable2)

        restorables = backup_controller.pop_all()

        assert restorables == [restorable2, restorable1]

    def test_pop_to_partition(self, backup_controller):
        restorable1 = MinimalRestorable()
        restorable2 = MinimalRestorable()

        backup_controller.push(restorable1)
        backup_controller.push(backup_controller.partition)
        backup_controller.push(restorable2)

        assert backup_controller._restorables == [restorable1, backup_controller.partition, restorable2]

        backup_controller.pop_to_partition()

        assert backup_controller._restorables == [restorable1]

        backup_controller.pop_to_partition()

        assert backup_controller._restorables == []

    # End of tests that are for the 1.4 partition hack.


class TestRestorableRpmKey:
    gpg_key = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "../../data/version-independent/gpg-keys/RPM-GPG-KEY-redhat-release")
    )

    @pytest.fixture
    def rpm_key(self):
        return backup.RestorableRpmKey(self.gpg_key)

    def test_init(self):
        rpm_key = backup.RestorableRpmKey(self.gpg_key)

        assert rpm_key.previously_installed is None
        assert rpm_key.enabled is False
        assert rpm_key.keyid == "fd431d51"
        assert rpm_key.keyfile.endswith("/data/version-independent/gpg-keys/RPM-GPG-KEY-redhat-release")

    def test_installed_yes(self, run_subprocess_with_empty_rpmdb, rpm_key):
        utils.run_subprocess(["rpm", "--import", self.gpg_key], print_output=False)

        assert rpm_key.installed is True

    def test_installed_not_yet(self, run_subprocess_with_empty_rpmdb, rpm_key):
        assert rpm_key.installed is False

    def test_installed_generic_failure(self, monkeypatch, rpm_key):
        def run_subprocess_fail(*args, **kwargs):
            return "Unknown error", 1

        monkeypatch.setattr(utils, "run_subprocess", RunSubprocessMocked(return_value=("Unknown error", 1)))

        with pytest.raises(
            utils.ImportGPGKeyError, match="Searching the rpmdb for the gpg key fd431d51 failed: Code 1: Unknown error"
        ):
            rpm_key.installed

    def test_enable(self, run_subprocess_with_empty_rpmdb, rpm_key):
        rpm_key.enable()

        assert rpm_key.enabled is True
        assert rpm_key.installed is True
        assert rpm_key.previously_installed is False

    def test_enable_already_enabled(self, run_subprocess_with_empty_rpmdb, rpm_key):
        rpm_key.enable()
        previous_number_of_calls = run_subprocess_with_empty_rpmdb.call_count
        rpm_key.enable()

        # Check that we do not double enable
        assert run_subprocess_with_empty_rpmdb.call_count == previous_number_of_calls

        # Check that nothing has changed
        assert rpm_key.enabled is True
        assert rpm_key.installed is True
        assert rpm_key.previously_installed is False

    def test_enable_already_installed(self, run_subprocess_with_empty_rpmdb, rpm_key):
        utils.run_subprocess(["rpm", "--import", self.gpg_key], print_output=False)
        rpm_key.enable()

        # Check that we did not call rpm to import the key
        # Omit the first call because that is the call we performed to setup the test.
        for call in run_subprocess_with_empty_rpmdb.call_args_list[1:]:
            assert not (call[0][0] == "rpm" and "--import" in call[0])

        # Check that the key is installed and we show that it was previously installed
        assert rpm_key.enabled is True
        assert rpm_key.installed is True
        assert rpm_key.previously_installed is True

    def test_enable_failure_to_import(self, monkeypatch, run_subprocess_with_empty_rpmdb, rpm_key):
        # Raise an error when we try to rpm --import
        def run_subprocess_error(*args, **kwargs):
            if args[0][0] == "rpm" and "--import" in args[0]:
                return "Error importing", 1
            return run_subprocess_with_empty_rpmdb(*args, **kwargs)

        monkeypatch.setattr(utils, "run_subprocess", run_subprocess_error)

        with pytest.raises(utils.ImportGPGKeyError, match="Failed to import the GPG key [^ ]+: Error importing"):
            rpm_key.enable()

    def test_restore_uninstall(self, run_subprocess_with_empty_rpmdb, rpm_key):
        rpm_key.enable()

        rpm_key.restore()

        # Check that the beginning of the run_subprocess call starts with the command to remove
        # the key (The arguments our fixture has added to use the empty rpmdb come after that)
        assert run_subprocess_with_empty_rpmdb.call_args_list[-1][0][0][0:3] == ["rpm", "-e", "gpg-pubkey-fd431d51"]

        # Check that we actually removed the key from the rpmdb
        output, status = run_subprocess_with_empty_rpmdb(["rpm", "-qa", "gpg-pubkey"])
        assert output == ""

    def test_restore_not_enabled(self, run_subprocess_with_empty_rpmdb, rpm_key):
        called_previously = run_subprocess_with_empty_rpmdb.call_count
        rpm_key.restore()

        assert run_subprocess_with_empty_rpmdb.call_count == called_previously
        assert rpm_key.enabled is False

    def test_restore_previously_installed(self, run_subprocess_with_empty_rpmdb, rpm_key):
        utils.run_subprocess(["rpm", "--import", self.gpg_key], print_output=False)
        rpm_key.enable()
        called_previously = run_subprocess_with_empty_rpmdb.call_count

        rpm_key.restore()

        # run_subprocess has not been called again
        assert run_subprocess_with_empty_rpmdb.call_count == called_previously

        # Check that the key is still in the rpmdb
        output, status = run_subprocess_with_empty_rpmdb(["rpm", "-q", "gpg-pubkey-fd431d51"])
        assert status == 0
        assert output.startswith("gpg-pubkey-fd431d51")
        assert rpm_key.enabled is False


@pytest.mark.parametrize(
    ("pkg_nevra", "nvra_without_epoch"),
    (
        ("7:oraclelinux-release-7.9-1.0.9.el7.x86_64", "oraclelinux-release-7.9-1.0.9.el7.x86_64"),
        ("oraclelinux-release-8:8.2-1.0.8.el8.x86_64", "oraclelinux-release-8:8.2-1.0.8.el8.x86_64"),
        ("1:mod_proxy_html-2.4.6-97.el7.centos.5.x86_64", "mod_proxy_html-2.4.6-97.el7.centos.5.x86_64"),
        ("httpd-tools-2.4.6-97.el7.centos.5.x86_64", "httpd-tools-2.4.6-97.el7.centos.5.x86_64"),
    ),
)
def test_remove_epoch_from_yum_nevra_notation(pkg_nevra, nvra_without_epoch):
    assert backup.remove_epoch_from_yum_nevra_notation(pkg_nevra) == nvra_without_epoch