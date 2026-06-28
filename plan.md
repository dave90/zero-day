Look the project and I want to add this feature for orchestrator:
- possibility to add a new tools. The tool is a python script that will be created in zero_files and accessed and visible by orchestrator and sub agents. 
But the tools can be created only by orchestrator usefull to be used by subagents or orchestrator so without every time recreate the script. The script is persisted in the zero_files so when the agent is shut down in next startup can fetch it. So the script contains the description what it is when use it and
how to use it. Orchestrator can create how many tools want the important is that he will test it before
publish. So my idea is that we have following new tools:
- create custom tools
- list custom tools
- modify custom tools
- delete custom tools
- call custom tools
And orchestrator can only create/modify and delete the other subagent no, only list and call / use 