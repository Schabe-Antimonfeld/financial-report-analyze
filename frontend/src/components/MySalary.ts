import{ref}from "vue"
export default function(){
    const userName = ref('roy')
    const salary = ref(15000)
    function addSalary(){
        salary.value+=1000

    }
    return {userName,salary,addSalary}
}