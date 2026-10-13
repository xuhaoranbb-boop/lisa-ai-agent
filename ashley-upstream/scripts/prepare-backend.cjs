const fs = require('node:fs');
const path = require('node:path');

const ashleyRoot = path.resolve(__dirname, '..');
const projectRoot = path.resolve(ashleyRoot, '..');
const outputRoot = path.join(ashleyRoot, 'backend-bundle');
const venvRoot = path.join(projectRoot, '.venv');
const pyvenvConfig = path.join(venvRoot, 'pyvenv.cfg');

function readPythonHome() {
  if (!fs.existsSync(pyvenvConfig)) {
    throw new Error(`Missing virtual environment config: ${pyvenvConfig}`);
  }
  const line = fs
    .readFileSync(pyvenvConfig, 'utf8')
    .split(/\r?\n/)
    .find((item) => item.startsWith('home = '));
  if (!line) throw new Error('pyvenv.cfg does not contain a Python home.');
  const home = line.slice('home = '.length).trim();
  if (!fs.existsSync(path.join(home, 'python.exe'))) {
    throw new Error(`Bundled Python runtime not found: ${home}`);
  }
  return home;
}

function copyFilter(source) {
  const name = path.basename(source);
  if (name === '__pycache__') return false;
  if (name.endsWith('.pyc') || name.endsWith('.pyo')) return false;
  return true;
}

async function copyDirectory(source, destination) {
  await fs.promises.cp(source, destination, {
    recursive: true,
    force: true,
    filter: copyFilter,
    dereference: true,
  });
}

async function main() {
  const pythonHome = readPythonHome();
  await fs.promises.rm(outputRoot, { recursive: true, force: true });
  await fs.promises.mkdir(outputRoot, { recursive: true });

  await copyDirectory(pythonHome, path.join(outputRoot, 'python'));
  await copyDirectory(
    path.join(venvRoot, 'Lib', 'site-packages'),
    path.join(outputRoot, 'site-packages')
  );
  await copyDirectory(path.join(projectRoot, 'src'), path.join(outputRoot, 'src'));

  const versionPath = path.join(projectRoot, 'VERSION');
  if (fs.existsSync(versionPath)) {
    await fs.promises.copyFile(versionPath, path.join(outputRoot, 'VERSION'));
  }

  // Public packages carry configuration examples, never the developer's secrets.
  const exampleEnvironment = path.join(projectRoot, '.env.example');
  const bundledEnvironment = false;
  if (fs.existsSync(exampleEnvironment)) {
    await fs.promises.copyFile(exampleEnvironment, path.join(outputRoot, '.env.example'));
  }

  const piperSource = path.join(process.env.USERPROFILE || '', '.jarvis', 'piper');
  if (fs.existsSync(piperSource)) {
    await copyDirectory(piperSource, path.join(outputRoot, 'assets', 'piper'));
  }

  const whisperSource = path.join(
    process.env.USERPROFILE || '',
    '.cache',
    'huggingface',
    'hub'
  );
  if (fs.existsSync(whisperSource)) {
    await copyDirectory(
      whisperSource,
      path.join(outputRoot, 'assets', 'huggingface', 'hub')
    );
  }

  const manifest = {
    version: fs.existsSync(versionPath)
      ? fs.readFileSync(versionPath, 'utf8').trim()
      : 'unknown',
    pythonHome,
    createdBy: 'scripts/prepare-backend.cjs',
    includes: {
      python: true,
      sitePackages: true,
      source: true,
      environment: bundledEnvironment,
      piper: fs.existsSync(piperSource),
      whisper: fs.existsSync(whisperSource),
    },
  };
  await fs.promises.writeFile(
    path.join(outputRoot, 'backend-manifest.json'),
    JSON.stringify(manifest, null, 2),
    'utf8'
  );
  console.log(`Production backend staged at ${outputRoot}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
