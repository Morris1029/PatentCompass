import { readFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import * as echarts from 'echarts';
import sharp from 'sharp';

// Accept JSON configuration only. Never evaluate JavaScript from report content.
const args = process.argv.slice(2);
const inputIndex = args.indexOf('--input');
const outputIndex = args.indexOf('--output');
if (inputIndex < 0 || outputIndex < 0 || !args[inputIndex + 1] || !args[outputIndex + 1]) {
  console.error('Usage: node render.mjs --input option.json --output chart.png');
  process.exit(2);
}

let chart;
try {
  const source = JSON.parse(await readFile(resolve(args[inputIndex + 1]), 'utf8'));
  const option = source.option ?? source;
  if (!option || typeof option !== 'object' || Array.isArray(option)) {
    throw new Error('Chart option must be a JSON object');
  }
  const width = source.option ? (source.width ?? 1200) : 1200;
  const height = source.option ? (source.height ?? 640) : 640;
  if (![width, height].every((value) => Number.isInteger(value) && value >= 200 && value <= 4096)) {
    throw new Error('Chart width and height must be integers between 200 and 4096');
  }
  chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width, height });
  chart.setOption({
    ...option,
    animation: false,
    backgroundColor: option.backgroundColor ?? '#ffffff',
    textStyle: { fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, sans-serif', ...option.textStyle },
  });
  const svg = chart.renderToSVGString();
  const output = resolve(args[outputIndex + 1]);
  await mkdir(dirname(output), { recursive: true });
  await sharp(Buffer.from(svg)).png().toFile(output);
  process.stdout.write(JSON.stringify({ output, width, height }) + '\n');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  chart?.dispose();
}
