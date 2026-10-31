"""Local Git comparison checks source provenance without fetching or executing a Skill."""
import subprocess

import pytest

from scripts.verify_skill_source import verify


def _git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()


def test_exact_git_blob_match_and_modified_copy(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    _git(repo, 'init', '-q')
    skill = repo / 'skills' / 'sample' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: sample\ndescription: sample\n---\nOriginal.\n')
    _git(repo, 'add', 'skills/sample/SKILL.md')
    _git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
         'commit', '-qm', 'source')
    commit = _git(repo, 'rev-parse', 'HEAD')
    result = verify(repo, commit, 'skills/sample/SKILL.md', skill)
    assert result['matched'] is True
    assert result['document_sha256'] == result['upstream_sha256']
    skill.write_text(skill.read_text().replace('Original.', 'Changed.'))
    assert verify(repo, commit, 'skills/sample/SKILL.md', skill)['matched'] is False


@pytest.mark.parametrize('source_path', ['../secret', '/etc/passwd', 'x:y', 'skills//SKILL.md'])
def test_invalid_source_paths_rejected(tmp_path, source_path):
    (tmp_path / '.git').mkdir()
    skill = tmp_path / 'SKILL.md'
    skill.write_text('sample')
    with pytest.raises(ValueError, match='source_path'):
        verify(tmp_path, 'a' * 40, source_path, skill)
