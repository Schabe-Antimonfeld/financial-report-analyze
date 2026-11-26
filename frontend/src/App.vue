
<template>
 <div>
  name: <input v-model="userName"/>{{userName}}<br/>
  salary: <input v-model="salary" type = "number"> {{ salary }}<br/>
  <button v-on:click="addSalary">commit</button>
  
  </div>
</template>

<script setup lang="ts">
import Mysalary from './components/MySalary'
let {userName,salary,addSalary} = Mysalary()
</script>

<style scoped>
.userInfo{
  background-color: aqua;
  widows: 80%;
}
.userInfo span{
  background-color: yellow;
  margin-left: 10px;
  border: 1px;
  border-radius: 5px;
}
</style>
