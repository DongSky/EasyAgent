// SPDX-FileCopyrightText: 2026 EasyAgent contributors
// SPDX-License-Identifier: Apache-2.0

/** Native Node extension protocol. Use an isolated handle(request) function for pure JS. */
export class Extension {
  constructor(){this.handlers=new Map();}
  handle(name,handler){this.handlers.set(name,handler);return this;}
  async run({persistent=false}={}){
    const {createInterface}=await import('node:readline');
    const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
    for await(const line of lines){
      if(Buffer.byteLength(line)>2_000_000)throw Error('Request exceeds 2 MB');
      const request=JSON.parse(line);
      if(request.protocol_version!==1)throw Error('Unsupported protocol');
      const handler=this.handlers.get(request.method);
      if(!handler&&!request.method.startsWith('lifecycle.'))throw Error('Unknown handler');
      const result=handler?await handler(request.params,request.context):{};
      process.stdout.write(JSON.stringify(result instanceof ExtensionResponse?result.value:{result})+'\n');
      if(!persistent)break;
    }
    lines.close();
  }
}
export class ExtensionResponse {constructor(value){this.value=value;}}
