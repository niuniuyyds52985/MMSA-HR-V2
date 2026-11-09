'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const display=require('../site/js/text-display.js');
const data=JSON.parse(fs.readFileSync(path.join(__dirname,'../config/text-normalization.v1.json'),'utf8'));
test('reading projection simplifies mixed script without changing its input',()=>{
  const original={text:'我的目的是讓大家的能力可以發揮；我協調了場地。'};
  assert.equal(display.simplify(original.text),'我的目的是让大家的能力可以发挥；我协调了场地。');
  assert.equal(original.text,'我的目的是讓大家的能力可以發揮；我協調了場地。');
});
test('complete phrase dictionary including mixed forms retains upstream exceptions',()=>{
  for(const [source,target]of Object.entries(data.phrases)){
    assert.equal(display.simplify(source),target,source);
    assert.equal(display.simplify(target),target,source+' stable');
  }
});
test('no regional vocabulary replacement, number or punctuation rewrite',()=>{
  assert.equal(display.simplify('😀API v2：資訊、軟體，25.5%！'),'😀API v2：资讯、软体，25.5%！');
});
test('display selection resolves the immutable original quote and codepoint offsets',()=>{
  assert.deepEqual(display.resolveOriginalQuote('😀我協調場地。隊友負責物料。','我协调场地'),{quote:'我協調場地',char_start:1,char_end:6});
});
test('equivalent simplified selections never choose an ambiguous original',()=>{
  assert.equal(display.resolveOriginalQuote('我協調；我协调。','我协调'),null);
  assert.equal(display.resolveOriginalQuote('我協調。','我组织'),null);
  assert.equal(display.resolveOriginalQuote('我協調。',''),null);
});
test('all source/target mappings retain Unicode codepoint length including astral characters',()=>{
  for(const [source,target]of [...Object.entries(data.characters),...Object.entries(data.phrases)])assert.equal(Array.from(source).length,Array.from(target).length,source);
  const [astral,target]=Object.entries(data.characters).find(([source,value])=>source.length!==value.length);
  const raw='😀'+astral+'協調';
  assert.deepEqual(display.resolveOriginalQuote(raw,target+'协调'),{quote:astral+'協調',char_start:1,char_end:4});
});
test('display is empty-safe and metadata explicitly says originals are retained',()=>{
  assert.equal(display.simplify(null),'');assert.equal(display.simplify(undefined),'');
  assert.equal(display.metadata.version,data.version);assert.equal(display.metadata.originalUnchanged,true);
});
test('a selection inside a protected phrase uses the displayed context first',()=>{
  assert.deepEqual(display.resolveOriginalQuote('到乾清宮附近協調','乾'),{quote:'乾',char_start:1,char_end:2});
  assert.deepEqual(display.resolveOriginalQuote('到乾清宮附近協調','清宫'),{quote:'清宮',char_start:2,char_end:4});
});
