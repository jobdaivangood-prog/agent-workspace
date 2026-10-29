// One process, no subprocess isolation, no automatic deletion of observations.
import {parseArgs} from 'node:util';
const {values}=parseArgs({options:{suite:{type:'string'}}});
const suites={core:'../test/core.test.js',protocol:'../test/protocol.test.js',credentials:'../test/credentials.test.js',chatSource:'../test/chat-source.test.js',chatSync:'../test/chat-sync.test.js'};
if(values.suite){if(!suites[values.suite])throw new Error('Unknown suite');await import(suites[values.suite]);}
else{for(const module of Object.values(suites))await import(module);}
