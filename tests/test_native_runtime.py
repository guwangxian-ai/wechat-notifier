from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_native_bridge as runtime
import install_native_client as installer


class NativeRuntimeTests(unittest.TestCase):
    def test_missing_account_is_distinct_from_multiple_accounts(self):
        with patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(stdout='')):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_NOT_READY'):
                runtime.active_account(42)
        base=str(Path.home()/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files')
        files='\n'.join('n'+base+'/'+a+'/db_storage/contact/contact.db' for a in ('wxid_one_slot1','wxid_two_slot2'))
        with patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(stdout=files)):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_AMBIGUOUS'):
                runtime.active_account(42)

    def test_wrong_client_path_is_rejected_before_login_fallback(self):
        with patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(stdout='n/other/WeChat.app/Contents/MacOS/WeChat')):
            with self.assertRaisesRegex(RuntimeError,'OTHER_INSTALLATION'):
                runtime.active_account(42,'/Applications/WeChat.app')

    def test_bootstrap_requires_approved_account_and_exact_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'state';data.mkdir();home=root/'home'
            account=home/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_demo_slot1'
            contact=account/'db_storage/contact/contact.db';contact.parent.mkdir(parents=True);contact.write_bytes(b'synthetic-db')
            consent={'install_hook':True,'read_group_directory':True,'backup_account_data':True}
            record={'phase':'awaiting_login','wechat_pid':42,'expected_account':'wxid_demo','account_path':str(account)}
            journal=data/'native-directory-bootstrap.json'
            def save(c=consent,r=record):
                (data/'native-consent.json').write_text(json.dumps(c));journal.write_text(json.dumps(r));journal.chmod(0o600)
            save()
            with patch.object(runtime.Path,'home',return_value=home):
                self.assertEqual(runtime.bootstrap_account(data,42),(account,'wxid_demo'))
                with self.assertRaisesRegex(RuntimeError,'PROCESS_CHANGED'):runtime.bootstrap_account(data,43)
                save(r={**record,'expected_account':'wxid_other'})
                with self.assertRaisesRegex(RuntimeError,'ACCOUNT_CHANGED'):runtime.bootstrap_account(data,42)
                save(c={**consent,'read_group_directory':False})
                with self.assertRaisesRegex(RuntimeError,'APPROVAL_REQUIRED'):runtime.bootstrap_account(data,42)
                save(r={**record,'account_path':str(root/'unapproved-account')})
                with self.assertRaisesRegex(RuntimeError,'ACCOUNT_INVALID'):runtime.bootstrap_account(data,42)
                save();journal.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError,'JOURNAL_INVALID'):runtime.bootstrap_account(data,42)

    def test_update_settings_are_untouched_without_separate_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.state').mkdir()
            (root/'.state/native-consent.json').write_text(json.dumps({'install_hook':True,'pin_auto_updates':False}))
            with patch.object(installer,'ROOT',root),patch.object(installer.subprocess,'run') as execute:
                with self.assertRaisesRegex(RuntimeError,'PIN_APPROVAL_REQUIRED'):
                    installer.disable_auto_updates(root)
                execute.assert_not_called()
