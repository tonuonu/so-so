function parseCSV(text) {
  return text.split('\n').map(line => line.split(','));
}
module.exports = { parseCSV };
