const fs = require('fs');

function lookupVersion(version, lookupPath) {
  const lines = fs.readFileSync(lookupPath, 'utf8').split('\n');

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('license:')) continue;

    const [entryVersion, formatStr] = trimmed.split(',').map(s => s.trim());
    if (entryVersion === version) {
      return { format: parseInt(formatStr, 10) };
    }
  }

  if (version === '1.0-1.5.x') {
    return { format: null }; // pre-1.6 era, no format number exists
  }

  throw new Error(`No lookup entry found for version "${version}"`);
}

module.exports = { lookupVersion };