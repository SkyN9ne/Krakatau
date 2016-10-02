import ast
import collections
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from Krakatau import script_util
from Krakatau.assembler.tokenize import AsssemblerError
import decompile
import disassemble
import assemble
import tests

# Note: If this script is moved, be sure to update this path.
krakatau_root = os.path.dirname(os.path.abspath(__file__))
cache_location = os.path.join(krakatau_root, 'tests', '.cache')
dec_class_location = os.path.join(krakatau_root, 'tests', 'decompiler', 'classes')
dis_class_location = os.path.join(krakatau_root, 'tests', 'disassembler', 'classes')

class TestFailed(Exception):
    pass

def execute(args, cwd):
    print 'executing command', args, 'in directory', cwd
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    return process.communicate()

def read(filename):
    with open(filename, 'rb') as f:
        return f.read()

def shash(data): return hashlib.sha256(data).hexdigest()

###############################################################################
def _runJava(target, in_fname, argslist):
    tdir = tempfile.mkdtemp()
    with open(in_fname, 'rb') as temp:
        isclass = temp.read(4) == b'\xCA\xFE\xBA\xBE'

    if isclass:
        shutil.copy2(in_fname, os.path.join(tdir, target + '.class'))
        func = lambda args: execute(['java', target] + list(args), cwd=tdir)
    else:
        shutil.copy2(in_fname, os.path.join(tdir, target + '.jar'))
        func = lambda args: execute(['java', '-cp', target + '.jar', target] + list(args), cwd=tdir)

    for args in argslist:
        # results = execute(['java', target] + list(args), cwd=tdir)
        results = func(args)
        assert 'VerifyError' not in results[1]
        assert 'ClassFormatError' not in results[1]
        yield results
    shutil.rmtree(tdir)

def runJava(target, in_fname, argslist):
    digest = shash(read(in_fname) + json.dumps(argslist).encode())
    cache = os.path.join(cache_location, digest)
    try:
        with open(cache, 'r') as f:
            return json.load(f)
    except IOError:
        print 'failed to load cache', digest

    results = list(_runJava(target, in_fname, argslist))
    with open(cache, 'w') as f:
        json.dump(results, f)
    # reparse json to ensure consistent results in 1st time vs cache hit
    with open(cache, 'r') as f:
        return json.load(f)

def compileJava(target, in_fname):
    assert not in_fname.endswith('.class')
    digest = shash(read(in_fname))
    cache = os.path.join(cache_location, digest)

    if not os.path.exists(cache):
        tdir = tempfile.mkdtemp()
        shutil.copy2(in_fname, os.path.join(tdir, target + '.java'))

        _, stderr = execute(['javac', target + '.java', '-g:none'], cwd=tdir)
        if 'error:' in stderr: # Ignore compiler unchecked warnings by looking for 'error:'
            raise TestFailed('Compile failed: ' + stderr)
        shutil.copy2(os.path.join(tdir, target + '.class'), cache)

        shutil.rmtree(tdir)
    return cache

def runJavaAndCompare(target, testcases, good_fname, new_fname):
    expected_results = runJava(target, good_fname, testcases)
    actual_results = runJava(target, new_fname, testcases)

    for args, expected, actual in zip(testcases, expected_results, actual_results):
        if expected != actual:
            message = ['Failed test {} w/ args {}:'.format(target, args)]
            if actual[0] != expected[0]:
                message.append('  expected stdout: ' + repr(expected[0]))
                message.append('  actual stdout  : ' + repr(actual[0]))
            if actual[1] != expected[1]:
                message.append('  expected stderr: ' + repr(expected[1]))
                message.append('  actual stderr  : ' + repr(actual[1]))
            raise TestFailed('\n'.join(message))

def runDecompilerTest(target, testcases):
    print 'Running decompiler test {}...'.format(target)
    tdir = tempfile.mkdtemp()

    cpath = [decompile.findJRE(), dec_class_location]
    if cpath[0] is None:
        raise RuntimeError('Unable to locate rt.jar')

    decompile.decompileClass(cpath, targets=[target], outpath=tdir, add_throws=True)
    new_fname = compileJava(target, os.path.join(tdir, target + '.java'))

    # testcases = map(tuple, tests.decompiler.registry[target])
    good_fname = os.path.join(dec_class_location, target + '.class')
    runJavaAndCompare(target, testcases, good_fname, new_fname)
    shutil.rmtree(tdir)

def runDisassemblerTest(target, testcases):
    print 'Running disassembler test {}...'.format(target)
    tdir = tempfile.mkdtemp()

    classloc = os.path.join(dis_class_location, target + '.class')
    jarloc = os.path.join(dis_class_location, target + '.jar')
    isjar = not os.path.exists(classloc)

    contents = collections.OrderedDict()
    if isjar:
        with zipfile.ZipFile(jarloc, 'r') as archive:
            for name in archive.namelist():
                if not name.endswith('.class'):
                    continue
                name = name[:-len('.class')]
                assert name not in contents
                with archive.open(name + '.class') as f:
                    contents[name] = f.read()
        good_fname = jarloc
    else:
        with open(classloc, 'rb') as f:
            contents[target] = f.read()
        good_fname = classloc

    with script_util.MockWriter() as out:
        disassemble.disassembleSub(contents.get, out, list(contents))
        disassembled = collections.OrderedDict(out.results)
        assert out.results == disassembled.items()

    assembled = collections.OrderedDict()
    for name, classfile in contents.items():
        source = disassembled[name]
        for name2, data in assemble.assembleSource(source, name, fatal=True):
            assert name == name2
            assert len(data) <= len(classfile)
            assembled[name] = data

    if isjar:
        new_fname = os.path.join(tdir, target + '.jar')
        with script_util.JarWriter(new_fname, '.class') as out:
            for cname, data in assembled.items():
                out.write(cname, data)
    else:
        # new_fname = os.path.join(tdir, target + '.class')
        with script_util.DirectoryWriter(tdir, '.class') as out:
            new_fname = out.write(target, assembled[target])

    runJavaAndCompare(target, testcases, good_fname, new_fname)
    shutil.rmtree(tdir)

PP_MARKER = b'###preprocess###\n'
RANGE_RE = re.compile(br'###range(\([^)]+\)):')
def preprocess(source, fname):
    if source.startswith(PP_MARKER):
        print 'Preprocessing', fname
        buf = bytearray()
        pos = len(PP_MARKER)
        dstart = source.find(b'###range', pos)
        while dstart != -1:
            buf += source[pos:dstart]
            dend = source.find(b'###', dstart + 3)
            m = RANGE_RE.match(source, dstart, dend)
            pattern = source[m.end():dend]
            for i in range(*ast.literal_eval(m.group(1))):
                buf += pattern.format(i, ip1=i+1)
            pos = dend + 3
            dstart = source.find(b'###range', pos)
        buf += source[pos:]
        source = str(buf)
        # with open('temp/' + os.path.basename(fname), 'wb') as f:
        #     f.write(source)
    return source.decode('utf8')

def runAssemblerTest(fname, exceptFailure):
    basename = os.path.basename(fname)
    print 'Running assembler test', basename
    with open(fname, 'r') as f: # not unicode
        source = f.read()
    source = preprocess(source, fname)

    error = False
    try:
        assemble.assembleSource(source, basename, fatal=True)
    except AsssemblerError:
        error = True
    assert error == exceptFailure

def runTest(data):
    try:
        {
            'decompiler': runDecompilerTest,
            'disassembler': runDisassemblerTest,
            'assembler': runAssemblerTest,
        }[data[0]](*data[1:])
    except Exception:
        import traceback
        return 'Test {} failed:\n'.format(data) + traceback.format_exc()

def addAssemblerTests(testlist, basedir, exceptFailure):
    for fname in os.listdir(basedir):
        if fname.endswith('.j'):
            testlist.append(('assembler', os.path.join(basedir, fname), exceptFailure))

if __name__ == '__main__':
    do_decompile = 'd' in sys.argv[1] if len(sys.argv) > 1 else True
    do_disassemble = 's' in sys.argv[1] if len(sys.argv) > 1 else True
    do_assemble = 'a' in sys.argv[1] if len(sys.argv) > 1 else True
    specified_target = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        os.mkdir(cache_location)
    except OSError:
        pass

    start_time = time.time()
    testlist = []

    if do_decompile:
        for target, testcases in sorted(tests.decompiler.registry.items()):
            testlist.append(('decompiler', target, map(tuple, testcases)))
    if do_disassemble:
        for target, testcases in sorted(tests.disassembler.registry.items()):
            if specified_target is None or specified_target == target:
                testlist.append(('disassembler', target, map(tuple, testcases)))

    if do_assemble:
        test_base = os.path.join(krakatau_root, 'tests')
        addAssemblerTests(testlist, os.path.join(test_base, 'assembler', 'bad'), True)
        addAssemblerTests(testlist, os.path.join(test_base, 'assembler', 'good'), False)
        addAssemblerTests(testlist, os.path.join(test_base, 'decompiler', 'source'), False)
        addAssemblerTests(testlist, os.path.join(test_base, 'disassembler', 'source'), False)

    print len(testlist), 'test cases found'
    assert testlist
    for error in multiprocessing.Pool(processes=5).map(runTest, testlist):
    # for error in map(runTest, testlist):
        if error:
            print error
            break
    else:
        print 'All {} tests passed!'.format(len(testlist))
        print 'elapsed time:', time.time()-start_time
