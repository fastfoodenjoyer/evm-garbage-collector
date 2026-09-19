from evm_inventory.cli import main


def test_dry_run_no_db(tmp_path,capsys):
    wallets=tmp_path/'wallets.txt';wallets.write_text('0x'+'1'*40+'\n')
    db=tmp_path/'db.sqlite'
    assert main(['scan','--wallets',str(wallets),'--db',str(db),'--dry-run'])==0
    assert not db.exists()
    assert 'mandatory_checks' in capsys.readouterr().out


def test_invalid_wallet_fails_before_db(tmp_path):
    wallets=tmp_path/'wallets.txt';wallets.write_text('not an address')
    db=tmp_path/'db.sqlite'
    assert main(['scan','--wallets',str(wallets),'--db',str(db)])==2
    assert not db.exists()
